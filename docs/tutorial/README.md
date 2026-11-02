# 从零手搓 AI 复试面试官：教学文档总览

这套文档的目标不是“解释一个现成项目”，而是带你**从空目录开始，一层一层把一个能真实说话的 AI 面试官搭出来**。每一章都回答三个问题：

1. 这一层为什么存在？（架构动机）
2. 这一层由哪些文件和函数组成？（结构）
3. 我该用哪些命令、按什么顺序把它跑起来？（操作）

建议按顺序阅读；如果只想快速跑通，先看 [09-build-from-scratch.md](09-build-from-scratch.md) 的里程碑，再回头补原理。

---

## 0. 最终成品长什么样

```text
┌──────────────────────────── 浏览器 ────────────────────────────┐
│ frontend/index.html + styles.css + app.js                      │
│  · 大厅：选择 quick / real                                     │
│  · 考场：AI 状态、实时转写、阶段进度、麦克风、成绩单           │
│  · ?demo=1：不连 LiveKit 也能完整演示                          │
└───────────────┬───────────────────────────────▲────────────────┘
                │ 1. GET /api/token             │ 5. 音频 + data(topic=interview)
                ▼                               │
┌───────────────────────────┐        ┌──────────┴─────────────────┐
│ server.py  (:8080)        │        │ LiveKit Room               │
│  · 静态文件               │        │  · WebRTC 音频             │
│  · 签发 JWT               │        │  · 数据通道                │
│  · /api/health            │        └──────────▲─────────────────┘
└───────────────────────────┘                   │
                                                │ 音频/事件
┌───────────────────────────────────────────────┴────────────────┐
│ agent.py  (LiveKit Agents worker)                              │
│  · entrypoint：等考生、读 metadata、建会话                     │
│  · Silero VAD → STT(SenseVoice) → LangGraphLLM → TTS(CosyVoice)│
│  · session.say() 说开场白                                      │
└───────────────────────────┬────────────────────────────────────┘
                            │ chat()
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ langgraph_adapter.py                                           │
│  · 把 LiveKit 的 LLM 接口翻译成一次 LangGraph 调用             │
│  · custom 流 → 逐 token 给 TTS；values 流 → 最终状态           │
│  · 把 phase / status / report 发回浏览器                       │
└───────────────────────────┬────────────────────────────────────┘
                            │ astream()
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ interview_graph.py                                             │
│  self_intro → pro_qa → compre_qa → completed                   │
│  判分、追问、出题、成绩单；MemorySaver 按 thread_id 存档       │
└────────────────────────────────────────────────────────────────┘
```

三条最重要的“流”：

| 流 | 路径 | 说明 |
| --- | --- | --- |
| 音频上行 | 浏览器麦克风 → LiveKit → agent VAD/STT | 考生说话变成文字 |
| 决策 | agent → adapter → LangGraph | 文字进入状态机，决定下一句 |
| 音频下行 + UI | LangGraph 文本 → TTS → LiveKit → 浏览器；同时 data channel 推事件 | 考生听到考官，也看到阶段/成绩单 |

---

## 1. 阅读路线

| 章节 | 你会得到什么 | 预计时间 |
| --- | --- | --- |
| [01-mental-model.md](01-mental-model.md) | 五层架构、三条流、为什么选 LiveKit + LangGraph | 25 min |
| [02-scaffold-and-config.md](02-scaffold-and-config.md) | 空目录、依赖、`.env`、`config.py`、`interview_modes.py` | 30 min |
| [03-token-server.md](03-token-server.md) | 一个安全的小 HTTP 服务：静态文件 + JWT | 45 min |
| [04-interview-graph.md](04-interview-graph.md) | 面试状态机：状态、节点、路由、提示词、成绩单 | 70 min |
| [05-livekit-adapter.md](05-livekit-adapter.md) | 把 LangGraph 伪装成 LiveKit 的 LLM | 50 min |
| [06-agent-session.md](06-agent-session.md) | VAD/STT/TTS、会话生命周期、metadata、CLI | 50 min |
| [07-frontend.md](07-frontend.md) | 无构建前端：连接、转写、阶段、成绩单、演示模式 | 70 min |
| [08-testing-debugging.md](08-testing-debugging.md) | 假 LLM、假图、HTTP 测试、排障手册 | 45 min |
| [09-build-from-scratch.md](09-build-from-scratch.md) | 13 个里程碑（0–12），从空目录到完整系统 | 180 min |
| [10-multimodal-upgrade.md](10-multimodal-upgrade.md) | 多模态升级：语音到语音、副语言、视频信号 | 60 min |

配套报告：

- [../reports/architecture-review.md](../reports/architecture-review.md)：本次架构审查、已修复问题、验证记录。
- [../reports/multimodal-alternatives.md](../reports/multimodal-alternatives.md)：多模态替代方案对比（**只给方案，未改代码**）。

---

## 2. 前置知识

不需要会 React、不需要构建工具，但需要：

- Python 3.10+（本项目在 3.12 上验证）
- 基础 `asyncio`：知道 `async def`、`await`、`asyncio.Lock` 即可
- 基础 HTTP / JSON / JWT 概念
- 浏览器 `fetch`、事件监听、DOM
- 一个能跑 `livekit-server --dev` 的终端（没有也能用 `?demo=1` 看界面）

可选但很有帮助：

- 读过 LangGraph 的 `StateGraph` 和 `astream(stream_mode=...)`
- 了解 LiveKit 的 Room / Participant / Track 概念

---

## 3. 项目目录（教学版）

```text
ai-interviewer/
├── config.py                 # 所有环境变量 → 一个冻结 dataclass
├── interview_modes.py        # quick/real 的唯一事实来源（无第三方依赖）
├── interview_graph.py        # LangGraph 状态机 + 提示词 + 成绩单
├── langgraph_adapter.py      # LangGraph ↔ LiveKit LLM 适配器
├── agent.py                  # LiveKit worker：VAD/STT/TTS + 会话入口
├── server.py                 # 静态前端 + token 签发 + /api/health
├── frontend/
│   ├── index.html            # 结构 + SVG 图标精灵
│   ├── styles.css            # 深色、克制、响应式
│   └── app.js                # 单文件 IIFE：连接、转写、成绩单、演示
├── tests/
│   ├── test_graph_nodes.py
│   ├── test_graph_structure.py
│   ├── test_graph_flow.py
│   ├── test_langgraph_adapter.py
│   ├── test_agent_stream.py
│   └── test_server.py
├── docs/
│   ├── tutorial/             # 你正在读的这套
│   └── reports/              # 架构审查与多模态报告
├── requirements.txt
├── .env.example
└── README.md
```

---

## 4. 最小命令清单

```bash
python -m venv .venv
```

```bash
# Windows
.venv\Scripts\activate
# Linux / WSL
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env   # 然后填入 LLM / SiliconFlow Key
```

```bash
pytest tests/ -q
```

```bash
# 终端 1：本地 LiveKit
livekit-server --dev
```

```bash
# 终端 2：前端 + token 服务
python server.py
```

```bash
# 终端 3：AI 考官
python agent.py connect --room ai-room
```

浏览器打开 `http://localhost:8080`。没有 LiveKit / Key 时打开 `http://localhost:8080/?demo=1`。

---

## 5. 学习方式建议

每一章末尾都有“检查点”。不要只看，至少做三件事：

1. **手打代码**：不要复制整段；先写函数签名，再填实现。
2. **跑测试**：每完成一个模块就跑对应测试，红→绿。
3. **改一个小东西**：例如把每阶段题量从 1 改成 2、把成绩单多一个维度、把开场白换掉，观察哪些层需要同步修改。

这种“改一处、看整条链”的练习，比读十遍架构图更能建立系统直觉。

---

## 6. 术语表

| 术语 | 在本项目里的含义 |
| --- | --- |
| Room | LiveKit 的一个房间；本项目固定 `ai-room` |
| Participant | 房间成员；考生 identity 形如 `candidate-xxxx`，AI 是另一个 participant |
| Track | 一条媒体流；本项目只订阅音频 |
| Token | 服务端签发的 JWT，带 `roomJoin` 权限和 metadata |
| metadata | token 里的 JSON 字符串；本项目放 `interview_mode` 和 `session_id` |
| VAD | 语音活动检测；Silero 判断“开始/结束说话” |
| STT | 语音转文字；SenseVoiceSmall |
| TTS | 文字转语音；CosyVoice2 |
| Adapter | `LangGraphLLM`，让 LiveKit 以为 LangGraph 是一个普通 LLM |
| StateGraph | LangGraph 的状态机；节点读写共享 state |
| Checkpointer | 按 `thread_id` 保存每轮 state 的存储；本项目用内存版 |
| Phase | 面试阶段：`self_intro` / `pro_qa` / `compre_qa` / `completed` |
| Mode | 面试模式：`quick` / `real` |
| Data channel | LiveKit 的可靠消息通道；本项目 topic=`interview`，传 UI 事件 |

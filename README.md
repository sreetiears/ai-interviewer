# AI Interviewer（考研复试 AI 模拟面试系统）

基于 **LiveKit 实时音频流** + **LangGraph 状态机** + **SiliconFlow 语音合成与识别（STT / TTS）** 构建的考研复试模拟面试系统。浏览器打开即用，无需手工粘贴 LiveKit 密钥。

---

## ✨ 功能特性

### 面试流程

- **LangGraph 状态机驱动**四阶段面试：自我介绍 → 专业知识问答 → 综合素质考察 → 成绩报告。
- **两种面试模式**，可在页面进入考场前选择：

  | 模式 | 题量 | 追问 | 适用场景 |
  | --- | --- | --- | --- |
  | **简单模式（quick，默认）** | 每阶段 1 道主问题 | 最多 1 次 | 演示录像、快速体验（约 5–6 轮结束） |
  | **实战模式（real）** | 每阶段 3 道主问题 | 最多 2 次 | 完整模拟面试（约 15+ 轮） |

  模式通过 `/api/token?mode=quick|real` 写入 LiveKit token metadata，Agent 入房时读取；也可用环境变量 `INTERVIEW_MODE` 设置默认值。
- 自我介绍过短会被引导补充（有重试上限，不会卡死流程）；专业问答与综合素质阶段支持"一针见血"追问。
- 面试结束在最后一轮作答后立即生成**多维度成绩单**（总分 + 三方向评分 + 核心不足 + 改进建议 + 结语），无需考生再说话触发。

### 实时体验（前端）

- **实时转写**：基于 LiveKit `TranscriptionReceived`，考生与考官分栏显示、分片增量更新、智能自动滚动（用户上翻时暂停）。
- **阶段进度条**由后端数据通道事件驱动，随面试推进高亮；成绩单阶段自动弹出评分面板。
- **AI 状态可视化**：思考中（三点动画）→ 发言中 → 倾听作答中；考官/考生头像发声光圈。
- 麦克风静音开关、连接质量指示、断线重连提示、自动播放被浏览器拦截时的一键恢复、内联错误卡片 + 重试（不再使用 `alert`）。
- 响应式布局：360 / 390 / 768 / 1440 宽度均无横向溢出；`prefers-reduced-motion`、`aria-live`、`focus-visible` 无障碍支持。
- **演示模式**：URL 加 `?demo=1`（可叠加 `&mode=real|quick`、`&scene=report|connected`）即可在**没有 LiveKit / 没有 API Key** 的情况下完整演示整场面试，适合录屏与离线演示。
- LiveKit 浏览器 SDK **按需懒加载**：SDK 或后端不可用时页面不会白屏，演示模式仍可用。

### 工程与可靠性

- **前后端数据通道协议**（LiveKit data channel，topic = `interview`，UTF-8 JSON）：

  | 事件 | 载荷 | 用途 |
  | --- | --- | --- |
  | `status` | `{"state": "thinking"\|"listening"\|"speaking"}` | AI 状态指示 |
  | `phase` | `{"phase": "self_intro"\|"pro_qa"\|"compre_qa"\|"completed"}` | 阶段进度条 |
  | `report` | `{"report": {total_score, scores{...}, core_weakness, improvement_tips, closing}, "text": "纯文本成绩单"}` | 成绩单面板（每场只发一次） |
  | `mode` | `{"mode": "quick"\|"real"}` | 当前面试模式徽章 |

- **每场面试独立存档**：`/api/token` 为每个 token 生成一次性 `session_id`，写入 LiveKit metadata；Agent 用它作为 LangGraph 的 `thread_id`。同一考生重进考场不会继承上一场的状态。
- **真流式回复**：图节点通过 LangGraph `custom` stream 逐 token 推送，TTS 首字延迟显著低于"整段生成后再切块"。
- 关闭 `preemptive_generation`，避免推测执行导致一轮作答被重复消费（面试状态机有副作用，正确性优先）。
- 出题时会带上"已经问过的问题"，降低 real 模式中重复出题的概率。
- 服务端加固：`ThreadingHTTPServer`、路径穿越防护（unquote + realpath + commonpath）、移除通配 CORS（可选 `ALLOWED_ORIGIN` 白名单）、安全响应头、`Content-Length`/MIME/缓存策略、`GET /api/health`、token 默认 1 小时 TTL、UTF-8 安全的日志（中文 Windows 控制台不再因 emoji 崩溃）、端口占用友好报错与优雅退出。
- 测试：148 个测试（状态机节点、真实图多轮流转、两种模式题量、LangGraph↔LiveKit 适配器事件与流式、Agent 装配与模式解析、HTTP 服务端、配置预检、模式常量一致性），全部离线可跑。

---

## 🛠️ 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / WSL:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

核心配置项（完整列表见 `.env.example`）：

```ini
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

# LLM 核心驱动（OpenAI 兼容服务，如 SiliconFlow / DeepSeek / Qwen）
LLM_API_KEY=your_llm_api_key_here
LLM_BASE_URL=https://api.siliconflow.cn/v1
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
LLM_TIMEOUT_SECONDS=30
LLM_MAX_RETRIES=2

# 语音 STT / TTS（SiliconFlow）
SILICONFLOW_API_KEY=your_siliconflow_api_key_here
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
STT_MODEL=FunAudioLLM/SenseVoiceSmall
TTS_MODEL=FunAudioLLM/CosyVoice2-0.5B
TTS_VOICE=FunAudioLLM/CosyVoice2-0.5B:alex

# 面试模式与题量（可选）
INTERVIEW_MODE=quick
QUICK_QUESTIONS_PER_PHASE=1
REAL_QUESTIONS_PER_PHASE=3
TOKEN_TTL_SECONDS=3600

# 开场白（直接说出的台词，不是给模型的指令）
# GREETING_TEXT=同学你好，欢迎参加考研复试模拟面试，请先进行1分钟左右的自我介绍。
```

### 3. 运行测试

```bash
python -m pytest tests/
```

### 4. 本地启动

**Windows 一键演示（推荐）**：先预检再开三个窗口（LiveKit / 前端 / Agent）：

```powershell
.\scripts\demo-preflight.ps1
.\scripts\start-demo.ps1
```

Agent 启动前会检查 `LLM_API_KEY` / `SILICONFLOW_API_KEY`；缺 Key 会直接失败并提示，避免上台才发现。大厅页也会根据 `/api/health` 显示配置降级提示。

也可手动开三个终端：

#### 终端 1：启动 LiveKit Server

```bash
livekit-server --dev
```

#### 终端 2：启动定制前端与入场服务

```bash
python server.py
```

浏览器访问 `http://localhost:8080`，选择面试模式后点击**"进入考场"**，系统自动签发 token、加入房间并申请麦克风。

> 没有 LiveKit / API Key 也能看效果：访问 `http://localhost:8080/?demo=1`（演示模式），或 `?demo=1&mode=real` 看实战模式。

#### 终端 3：启动 AI 面试官 Agent

```bash
python agent.py connect --room ai-room
```

> 若 `livekit-server` 不在 PATH，可参考 `scripts/install_livekit_tools.sh`（Linux/WSL）。

---

## 📁 项目结构

```
.
├── agent.py                  # LiveKit 语音交互 Agent 入口（模式解析、事件发布）
├── config.py                 # 集中化配置（零硬编码）
├── interview_modes.py        # quick/real 的唯一事实来源（零第三方依赖）
├── interview_graph.py        # LangGraph 复试状态图、提示词、模式策略、成绩单
├── langgraph_adapter.py      # LangGraph ↔ LiveKit LLM 适配器（流式 + UI 事件）
├── server.py                 # 静态前端服务 + token 签发 + /api/health
├── scripts/
│   ├── demo-preflight.ps1    # Windows 演示预检
│   └── start-demo.ps1        # Windows 三窗口一键启动
├── frontend/
│   ├── index.html            # 面试工作台结构（无内联脚本/样式）
│   ├── styles.css            # 克制的极简深色主题 + 响应式 + 动效
│   └── app.js                # LiveKit 连接、转写、阶段、成绩单、模式选择、演示模式
├── tests/
│   ├── test_graph_nodes.py        # 状态机节点单测
│   ├── test_graph_structure.py    # 路由与图编译
│   ├── test_graph_flow.py         # 假 LLM 驱动完整多轮面试（含 real 模式/流式）
│   ├── test_langgraph_adapter.py  # 适配器事件契约与 token 流
│   ├── test_agent_stream.py       # Agent 装配、metadata 模式/会话解析
│   ├── test_interview_modes.py    # 模式常量唯一来源
│   └── test_server.py             # HTTP 服务端（65 项：路由/安全/缓存/模式）
├── scripts/                  # Linux/WSL 辅助脚本（安装 LiveKit、重建 venv、导入检查）
│   ├── install_livekit_tools.sh
│   ├── rebuild_venv.sh
│   ├── verify_agent_import.sh
│   ├── verify_livekit_compat.sh
│   └── verify_livekit_tools.sh
├── docs/
│   ├── tutorial/             # 从零手搓本项目的教学文档（10 章）
│   ├── reports/              # 架构审查、多模态方案、历史代码审查与优化记录
│   ├── adr/                  # 已落地的设计决定
│   └── screenshots/          # 优化前后对比截图
├── requirements.txt
├── .env.example
└── README.md
```

---

## 📚 教学文档

想从零理解或重写这个项目，从这里开始：

- [教学文档总览](docs/tutorial/README.md)：学习路线、三条数据流、术语表。
- [第 1 章：心智模型](docs/tutorial/01-mental-model.md)
- [第 2 章：搭骨架与配置](docs/tutorial/02-scaffold-and-config.md)
- [第 3 章：Token 服务与静态文件](docs/tutorial/03-token-server.md)
- [第 4 章：面试状态机](docs/tutorial/04-interview-graph.md)
- [第 5 章：LangGraph ↔ LiveKit 适配器](docs/tutorial/05-livekit-adapter.md)
- [第 6 章：语音会话入口](docs/tutorial/06-agent-session.md)
- [第 7 章：浏览器工作台](docs/tutorial/07-frontend.md)
- [第 8 章：测试与调试](docs/tutorial/08-testing-debugging.md)
- [第 9 章：从空目录手搓一遍](docs/tutorial/09-build-from-scratch.md)
- [第 10 章：多模态升级](docs/tutorial/10-multimodal-upgrade.md)

配套报告：

- [架构审查与优化报告](docs/reports/architecture-review.md)
- [多模态替代方案报告（未应用）](docs/reports/multimodal-alternatives.md)

---

## 🧭 面试流程图

```text
self_intro ──合格──▶ pro_qa ──题量用尽──▶ compre_qa ──题量用尽──▶ 成绩单（同一轮生成）
   │ 不合格且未达重试上限            ▲                    ▲
   └──引导补充，重新判断              └──追问 / 下一题 ─────┘
```

每个用户回合进入对应节点；`quick` 模式每阶段 1 题，`real` 模式每阶段 3 题、最多 2 次追问。

---

## ⚠️ 安全与部署提示

- `/api/token` 默认**不做身份鉴权**，只适合本机/内网演示；公网暴露前请加鉴权与限流，并把 `FRONTEND_HOST` 收紧到 `127.0.0.1`。
- `LIVEKIT_API_KEY/SECRET` 默认 `devkey/secret` 仅供 `livekit-server --dev`；生产必须替换为强随机密钥。
- token 默认 1 小时过期（`TOKEN_TTL_SECONDS`），每次进入考场都会重新签发；不要把 TTL 调得过长。
- 历史提交中曾出现明文 SiliconFlow API Key，若该 Key 仍有效请**立即吊销轮换**（详见 `docs/reports/code-review.md` P1-8）。
- 成绩单目前只在浏览器会话内展示，未落库；如需存档请自行接入持久化。

---

## 已知限制

- 本环境未运行 LiveKit Server，真实音频/麦克风链路的端到端验证需在具备 LiveKit 与 API Key 的机器上完成；前端已通过模拟事件 + 演示模式验证全部 UI 行为。
- 面试记录（`raw_scores`）与 LangGraph checkpoint 为进程内内存，worker 重启即丢失，多实例部署不共享状态。生产建议换 Postgres/Redis checkpointer。
- 当前只处理文本语义：STT 之后的语气、停顿、表情全部丢失；多模态升级方案见 [docs/reports/multimodal-alternatives.md](docs/reports/multimodal-alternatives.md)（未应用）。
- 前端为单文件 IIFE（约 1900 行），刻意保持零构建；继续扩张功能时建议拆 ES modules。

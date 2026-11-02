# 第 1 章：先建立心智模型

在写第一行代码之前，先把系统想清楚。否则你会陷入“每个文件都能看懂，但不知道它们为什么长这样”的状态。

---

## 1.1 这个系统到底在做什么

一句话：**浏览器里的考生说一句话，AI 考官听懂、决定下一句、说出来，并把过程同步到界面。**

把它拆成五个职责：

| 职责 | 谁负责 | 关键问题 |
| --- | --- | --- |
| 媒体传输 | LiveKit | 音频怎么从浏览器到服务端，再回来？ |
| 语音识别 | STT（SenseVoice） | 考生说了什么？ |
| 对话策略 | LangGraph | 现在该出题、追问、换阶段，还是出成绩单？ |
| 语音合成 | TTS（CosyVoice） | 考官这句话怎么说出来？ |
| 界面 | 原生前端 | 考生看到什么？阶段、转写、成绩单 |

难点不在任何单个职责，而在**它们之间的边界**：

- 音频是流式的，但状态机是回合制的；
- LLM 是逐 token 的，但 TTS 需要尽早开始；
- LangGraph 的 state 有副作用（每轮会推进面试），所以不能被“推测执行”重复消费；
- 浏览器的 UI 事件不应该混进音频流。

本项目用一个**适配器**（`LangGraphLLM`）把“回合制状态机”伪装成“流式 LLM”，这是整套架构的枢纽。

---

## 1.2 三条流

### 流 A：音频上行（考生 → 文字）

```text
浏览器麦克风
   │ WebRTC
   ▼
LiveKit Room
   │ 订阅音频
   ▼
agent.py 的 AgentSession
   │ Silero VAD：判断一句话开始/结束
   ▼
STT（OpenAI 兼容接口，SenseVoice）
   │ 文本
   ▼
chat_ctx 里出现一条 role="user" 的消息
```

关键点：**VAD 决定“一轮”什么时候结束**。本项目配置：

```python
vad=silero.VAD.load(
    min_speech_duration=0.6,   # 至少说 0.6 秒才算说话
    min_silence_duration=1.5,  # 静音 1.5 秒才算说完
)
```

这两个值直接决定“考生停顿多久会被当成说完”。太短会打断思考，太长会让考官反应慢。

### 流 B：决策（文字 → 下一句）

```text
AgentSession 调 llm.chat(chat_ctx)
   │
   ▼
LangGraphLLM.chat()          # 适配器入口
   │ 从 chat_ctx 取出最新一条 user 文本
   ▼
LangGraphLLMStream._run()
   │ graph.astream(input, config, stream_mode=["custom","values"])
   ▼
interview_graph.py
   │ 根据 thread_id 取出上一轮 state
   │ route_phase() 决定进入哪个节点
   │ 节点调 LLM 出题 / 判分 / 生成报告
   ▼
返回新的 state（reply、current_phase、report_data...）
```

关键点：**每一轮只推进一次状态机**。所以 `AgentSession(preemptive_generation=False)`，否则 LiveKit 可能为了降低延迟提前生成一份“草稿”，而这份草稿会再次推进面试状态。

### 流 C：输出（文字 → 声音 + 界面）

```text
graph 节点里每产生一个 token
   │ get_stream_writer()({"type":"delta","text":...})
   ▼
LangGraph 的 custom 流
   │
   ▼
适配器 _send_chunk(text)
   │
   ▼
TTS（CosyVoice）→ LiveKit → 浏览器扬声器

同时：
graph 最终 state 里的 current_phase / report_data
   │
   ▼
适配器 emit_event({"type":"phase"|"report"|"status"})
   │
   ▼
agent.py publish_event → LiveKit data channel（topic="interview"）
   │
   ▼
浏览器 handleDataMessage → 更新阶段条 / 弹出成绩单
```

关键点：**声音走音频轨，UI 走数据通道**。不要把阶段/成绩单塞进 TTS 台词里让前端去解析。

---

## 1.3 为什么是这五个技术

### 为什么用 LiveKit，而不是自己写 WebSocket 音频

自己写需要处理：WebRTC 协商、抖动缓冲、回声消除、断线重连、设备切换、多轨道订阅、服务端混流。LiveKit 把这些做完了，还提供 Python Agents 框架，直接给你 VAD/STT/LLM/TTS 的会话抽象。

代价：多一个服务进程（`livekit-server`），并且要理解 Room/Participant/Track 模型。

### 为什么用 LangGraph，而不是一堆 if/else

面试流程是**有状态、有分支、需要回放**的：

- 自我介绍不合格要重试，但有上限；
- 专业问答要判“是否需要追问”，追问次数有上限；
- 每阶段题量用尽才推进；
- 最后要在同一轮生成成绩单；
- 每轮都要带上前面的记录。

LangGraph 的 `StateGraph` 把这些显式化：state 是数据，节点是函数，路由是纯函数，checkpointer 负责按 `thread_id` 存档。测试时可以用假 LLM 驱动完整多轮流程。

代价：要理解 channel 合并语义（返回的 dict 是“增量更新”，未返回的字段保留旧值）。

### 为什么中间要一个 Adapter

LiveKit 的 `AgentSession` 只认识 `llm.LLM` 接口：你给它一个 `chat()`，它给你一个可迭代的 chunk 流。LangGraph 不是这个接口。

`LangGraphLLM` 做三件事：

1. 把 `chat_ctx` 翻译成 `{"user_query": ...}`；
2. 把 `graph.astream(...)` 的 `custom` 流翻译成 `ChatChunk`；
3. 把最终 state 翻译成 UI 事件。

没有它，你要么改 LiveKit 框架，要么把状态机逻辑塞进一个巨大的自定义 LLM 类。适配器是两者的“防腐层”。

### 为什么前端不用框架

这个界面只有几个状态：未连接 / 连接中 / 进行中 / 重连 / 结束 / 错误，加一个成绩单弹窗。用原生 JS + 一个 IIFE 足够，部署时不需要 Node、不需要打包、不需要 CDN 之外的依赖（LiveKit SDK 懒加载）。

代价：`app.js` 会比较长；所以它用大量分节注释组织，并且可以按“DOM 引用 / 工具 / 转写 / 数据通道 / 报告 / 连接 / 演示”顺序阅读。

### 为什么 STT/LLM/TTS 都走 OpenAI 兼容接口

SiliconFlow 提供 OpenAI 兼容的 `/v1/audio/transcriptions`、`/v1/chat/completions`、`/v1/audio/speech`。LiveKit 的 `openai` 插件可以直接指过去：

```python
openai.STT(base_url=..., api_key=..., model="FunAudioLLM/SenseVoiceSmall")
SiliconFlowTTS(base_url=..., api_key=..., model="FunAudioLLM/CosyVoice2-0.5B")
```

好处：换供应商通常只改 `base_url` / `model` / `voice`，不用改业务代码。代价：不同供应商对参数的支持不完全一致，需要少量适配（例如 TTS 文本清洗）。

---

## 1.4 状态机的心智模型

### 阶段与回合

```text
             ┌────────────── 不合格且还有重试次数 ──────────────┐
             ▼                                                │
      ┌─────────────┐   合格/重试用尽   ┌────────┐  题量用尽  ┌────────────┐
开始 →│ self_intro  │ ───────────────▶ │ pro_qa │ ─────────▶ │ compre_qa  │
      └─────────────┘                  └────────┘            └─────┬──────┘
                                          ▲   │ 追问/下一题          │ 最后一题答完
                                          └───┘                    ▼
                                                             ┌────────────┐
                                                             │ completed  │
                                                             │ + report   │
                                                             └────────────┘
```

注意几个反直觉的点：

1. **每个用户回合只进入一个节点**。节点既可能“出题”，也可能“评上一题”，靠 `is_answering` 区分。
2. `self_intro` 合格后，当前回合只说一句过渡语，**下一回合**才出第一道专业题。这是为了给考生一个自然的呼吸点。
3. `compre_qa` 的最后一题答完，**同一回合**直接生成成绩单，不进入一个“等待用户再说一句”的空阶段。
4. 面试结束后用户再说话，路由到 `completed` 节点，回一句“面试已经结束了”，而不是重播结语。

### 两种模式

| 模式 | 每阶段主问题 | 最大追问 | 自我介绍重试 | 用途 |
| --- | --- | --- | --- | --- |
| `quick` | 1 | 1 | 1 | 演示、录屏、快速体验 |
| `real` | 3 | 2 | 3 | 接近真实复试 |

模式不是写死在图里，而是：

```text
页面选择 → /api/token?mode=real → token metadata
        → agent 读 participant.metadata → LangGraphLLM(interview_mode)
        → 每轮 input_state 带 interview_mode
        → _interview_settings(state) 解析出题量/追问/重试上限
```

这条链路每一层都做一次 `normalize`，保证坏输入不会把面试卡死。

### Checkpoint 的边界

`MemorySaver` 按 `thread_id` 保存 state。`thread_id` 来自 token metadata 里的 `session_id`：

- 同一场面试的每一轮共享 state；
- 换一个 token（重新进入考场）就是新 `session_id`，不会继承上一场的 `completed` 状态；
- 旧客户端没有 `session_id` 时，退回 `房间名:identity`。

这也是为什么服务端要在 token 里放 `session_id`，而不是只用考生 identity。

---

## 1.5 一次完整回合的时序

以“专业问答第一题”为例：

```text
考生：好的老师，我准备好了。
  │
  ├─ 浏览器麦克风 → LiveKit → agent
  ├─ VAD 判定一句话结束
  ├─ STT → "好的老师，我准备好了。"
  │
  ▼
AgentSession 调 LangGraphLLM.chat(chat_ctx)
  │
  ├─ _extract_query → "好的老师，我准备好了。"
  ├─ emit status=thinking
  ├─ graph.astream({"user_query":..., "interview_mode":"quick"}, thread_id=...)
  │     │
  │     ├─ route_phase: state.current_phase == "pro_qa"
  │     ├─ node_pro_qa: is_answering=False → 出题
  │     │     ├─ _reply_text(llm, [PRO_QUESTION_SYSTEM, 已问问题])
  │     │     │     ├─ token "请" → custom delta → TTS 立即开始
  │     │     │     └─ ...
  │     │     └─ return {question_pro, reply, is_answering=True, asked_questions...}
  │     └─ values 流给出最终 state
  │
  ├─ 没有 token 流时（静态台词）→ 整段 _send_chunk(reply)
  ├─ emit phase=pro_qa（首次才发）
  ├─ emit status=speaking
  └─ TTS 把 reply 说完
  │
  ▼
浏览器：听到问题；阶段条高亮"专业问答"；转写区出现考官的话
```

---

## 1.6 失败模式与设计对策

| 失败 | 后果 | 本项目的对策 |
| --- | --- | --- |
| LLM 返回非 JSON 成绩单 | 伪造分数 | `_build_report_payload` 缺 `total_score` 直接抛错，前端显示“报告生成失败”，不编造分数 |
| LLM 卡住 | 考生干等 | `LLM_TIMEOUT_SECONDS` + `max_retries`；适配器 catch 后说“系统处理出现异常” |
| 自我介绍一直不合格 | 流程卡死 | `max_intro_attempts` 到顶强制进入下一阶段 |
| 预生成导致一轮被消费两次 | 题目跳转错乱 | `preemptive_generation=False` |
| 同一 identity 重进考场 | 继承上一场 completed | token 里带一次性 `session_id` |
| 面试结束后再说话 | 成绩单反复弹出 | 适配器 `_report_emitted` 只发一次 |
| 题目重复 | 体验差 | `asked_questions` 累积并带进下一次出题提示 |
| TTS 不支持空文本 | 请求失败 | `SiliconFlowTTS.synthesize` 清洗空白，空则回退原文 |
| 浏览器拦截自动播放 | 听不到考官 | 前端 `AudioPlaybackStatusChanged` + 一键恢复横幅 |
| 中文 Windows 控制台编码 | 启动崩溃 | `server.py` 用 logging + `backslashreplace`，不打印 emoji |

---

## 1.7 检查点

读完本章，你应该能不看代码回答：

1. 为什么 `preemptive_generation=False`？
2. `custom` 流和 `values` 流分别传什么？
3. 成绩单是在哪个节点、哪一回合生成的？
4. 为什么 token 里需要 `session_id`，只用 identity 不行吗？
5. 如果考生说完后停顿 1 秒，VAD 会认为他说完了吗？

下一章：从空目录开始搭骨架。

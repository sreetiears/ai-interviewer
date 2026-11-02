# 第 6 章：语音会话入口（agent.py）

前两章解决了“想什么”和“怎么说”。这一章解决“怎么听、什么时候说、在哪个房间说”。

`agent.py` 是 LiveKit Agents 的 worker 入口。它把 VAD、STT、适配器、TTS 组装成一个 `AgentSession`，并在考生进房后启动。

---

## 6.1 LiveKit Agents 的运行模型

两种常见启动方式：

```bash
python agent.py dev
```

```bash
python agent.py connect --room ai-room
```

### `dev` / worker 模式

`cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))` 会：

1. 连接 LiveKit 服务器；
2. 注册成一个 worker；
3. 等待 LiveKit 分配 job；
4. 每来一个房间，调用一次 `entrypoint(ctx)`。

适合：生产、多个房间、由 LiveKit 调度。

### `connect --room ai-room`

LiveKit CLI 会直接创建一个 job 并连接指定房间，不经过 worker 调度。适合：本地开发、单房间调试。

README 推荐本地用 `connect`，因为 `dev` 只注册 worker，不会主动进房，容易误以为“没反应”。

> 如果 `livekit-server` 不在 PATH，参考根目录的 `install_livekit_tools.sh`（Linux/WSL）。

---

## 6.2 `entrypoint` 总览

```python
async def entrypoint(ctx: JobContext) -> None:
    logger.info("Received interview job for room: %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("Connected to room, waiting for participant...")

    participant = await ctx.wait_for_participant()
    participant_identity = participant.identity or f"candidate-{uuid.uuid4().hex[:6]}"
    interview_mode = _resolve_interview_mode(participant)
    session_id = _resolve_session_id(participant)
    thread_id = session_id or f"{ctx.room.name}:{_safe_identity(participant_identity)}"
    ...
```

逐步解释：

| 步骤 | 作用 | 注意 |
| --- | --- | --- |
| `ctx.connect(auto_subscribe=AUDIO_ONLY)` | 以 agent 身份加入房间 | 只订阅音频，不要视频 |
| `wait_for_participant()` | 等第一个考生 | 已有人则立即返回 |
| `identity` | 用于显示和兜底 thread id | 可能为空，兜底生成 |
| `metadata` | 读 mode 和 session_id | 服务端签发时写入 |
| `thread_id` | LangGraph 存档 key | 优先 session_id |

### 为什么只订阅音频

`AutoSubscribe.AUDIO_ONLY` 避免订阅考生摄像头/屏幕共享。本项目只做语音；订阅视频会浪费带宽，也可能带来隐私问题。

### 为什么 `thread_id` 优先用 `session_id`

```python
thread_id = session_id or f"{ctx.room.name}:{_safe_identity(participant_identity)}"
```

- 新 token 一定带 `session_id`，每场面试独立；
- 旧客户端没有 `session_id` 时，用“房间 + 身份”兜底；
- 只用 identity 不够：同一考生重新进同一房间会继承上一场的 `completed` 状态。

---

## 6.3 读取 metadata

```python
def _participant_metadata(participant) -> dict:
    raw_metadata = getattr(participant, "metadata", "") or ""
    try:
        payload = json.loads(raw_metadata)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
```

LiveKit 把 token 里的 metadata 原样挂在 `participant.metadata`。可能是：

- 合法 JSON 对象；
- 空字符串；
- 非法 JSON（旧客户端/手工 token）。

一律回 `{}`，调用方再用默认值。

```python
def _resolve_session_id(participant) -> str:
    candidate = _participant_metadata(participant).get("session_id")
    if isinstance(candidate, str):
        candidate = candidate.strip()
        if SESSION_ID_RE.fullmatch(candidate):
            return candidate
    return ""
```

为什么校验字符集：`thread_id` 会进 checkpoint 的 key，虽然内存版不敏感，但未来换成数据库/Redis 时，任意字符串可能带来注入或长度问题。`[0-9A-Za-z_-]{1,128}` 足够。

```python
def _resolve_interview_mode(participant) -> str:
    payload = _participant_metadata(participant)
    mode = None
    for key in ("interview_mode", "mode"):
        if payload.get(key):
            mode = payload[key]
            break
    resolved = normalize_interview_mode(str(mode) if mode else config.interview_mode)
    if mode and resolved != str(mode).strip().lower():
        logger.warning("Unknown interview mode %r, falling back to %r", mode, resolved)
    return resolved
```

支持两个 key 是为了兼容旧 token。未知值回退并记 warning，不让坏客户端把面试搞崩。

---

## 6.4 组装会话：`_build_session`

```python
def _build_session(participant_identity, on_event=None, interview_mode=None, thread_id=None):
    user_id = _safe_identity(participant_identity)
    resolved_mode = normalize_interview_mode(interview_mode or config.interview_mode)
    resolved_thread = thread_id or user_id
    ...
    agent_instance = voice.Agent(instructions=config.agent_instructions)
    session = voice.AgentSession(
        preemptive_generation=False,
        vad=silero.VAD.load(
            min_speech_duration=0.6,
            min_silence_duration=1.5,
        ),
        stt=openai.STT(
            base_url=config.siliconflow_base_url,
            api_key=config.siliconflow_api_key or "sk-dummy",
            model=config.stt_model,
            language=config.stt_language,
        ),
        llm=LangGraphLLM(
            thread_id=resolved_thread,
            on_event=on_event,
            interview_mode=resolved_mode,
        ),
        tts=SiliconFlowTTS(
            base_url=config.siliconflow_base_url,
            api_key=config.siliconflow_api_key or "sk-dummy",
            model=config.tts_model,
            voice=config.tts_voice,
            response_format=config.tts_response_format,
        ),
    )
    return agent_instance, session
```

### `preemptive_generation=False`

LiveKit 支持“在考生还没说完时预先推测生成回复”，以降低延迟。但这个项目的图调用有副作用：

- 推进 `questions_asked`；
- 累加 `raw_scores`；
- 可能生成成绩单。

推测执行可能让同一轮被消费两次，导致题目跳转、记录重复。**正确性优先于几百毫秒延迟**。

### VAD 参数

```python
min_speech_duration=0.6
min_silence_duration=1.5
```

- 0.6 秒：避免咳嗽/键盘声被当成一句话；
- 1.5 秒：给考生思考停顿，不轻易打断。

如果觉得考官反应慢，先调小 `min_silence_duration`；如果经常把思考停顿当成说完，调大它。

### STT

```python
openai.STT(
    base_url=config.siliconflow_base_url,
    api_key=...,
    model="FunAudioLLM/SenseVoiceSmall",
    language="zh",
)
```

用 OpenAI 兼容插件指向 SiliconFlow。`language="zh"` 帮助识别中文；如果做中英混合面试，可以改成自动检测或按场次配置。

### TTS 与 `SiliconFlowTTS`

```python
class SiliconFlowTTS(openai_tts_impl.TTS):
    def synthesize(self, text, *, conn_options=...):
        cleaned_text = re.sub(r"\s+", " ", text).strip()
        return openai_tts_impl.AudioChunkedStream(
            tts=self,
            input_text=cleaned_text or text,
            conn_options=conn_options,
        )
```

为什么需要子类：

- 图里的台词可能带换行、连续空格；TTS 对空白敏感，清洗后更稳定；
- 如果清洗后为空，回退原文，避免空请求；
- 其余行为完全复用 OpenAI 兼容实现，不重复造轮子。

### `voice.Agent(instructions=...)`

严格说，`LangGraphLLM` 不读 `instructions`，它只看 `chat_ctx` 里的 user 消息。但 `AgentSession` 需要一个 Agent 对象。保留 instructions 是为了：

- 未来如果换成普通 LLM，可以直接用；
- 日志/调试时能看到人设；
- 不把“状态机提示词”和“Agent 人设”混在一起。

---

## 6.5 数据通道：把 UI 事件发出去

```python
async def publish_event(event: dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    try:
        await ctx.room.local_participant.publish_data(
            payload,
            reliable=True,
            topic=INTERVIEW_EVENT_TOPIC,
        )
    except Exception:
        logger.warning("Failed to publish interview event %r", event.get("type"), exc_info=True)
```

要点：

- `ensure_ascii=False`：中文按 UTF-8 发，不转义成 `\uXXXX`；
- `reliable=True`：阶段/成绩单不能丢；
- `topic="interview"`：前端只处理这个 topic；
- 发送失败只记日志，不影响面试音频。

这个函数作为 `on_event` 传给 `LangGraphLLM`。适配器每次 `emit_event`，最终都到这里。

---

## 6.6 启动会话与开场白

```python
logger.info("Starting voice session")
await session.start(agent=agent_instance, room=ctx.room)

await publish_event({"type": "mode", "mode": interview_mode})
await publish_event({"type": "phase", "phase": "self_intro"})

logger.info("Speaking opening greeting")
session.say(config.greeting_text, allow_interruptions=False)
```

### 为什么先发 mode/phase

前端可能在会话启动前就渲染了大厅；这两个事件让它立刻切到“面试中”的正确状态，而不是等第一句考官话才更新。

### 为什么用 `session.say` 而不是 `generate_reply`

历史做法：

```python
session.generate_reply(
    user_input=config.opening_prompt,   # "请先向考生打招呼……"
    allow_interruptions=False,
    input_modality="text",
)
```

这会把“指令”当成用户消息送进 `LangGraphLLM`，适配器再用字符串匹配拦截：

```python
if "打招呼" in self._query or "开始自我介绍" in self._query:
    self._send_chunk("同学你好……")
```

问题：

- 改 `OPENING_PROMPT` 的字就坏；
- 开场白被塞进图的状态机语义里（它不是考生回答）；
- 三处硬编码台词容易不同步。

现在：

- `config.greeting_text` 是**直接说出来的台词**；
- `session.say(...)` 绕过 LLM，直接走 TTS；
- `add_to_chat_ctx=True`（默认）会把开场白记入对话历史，但不会被当成 user 消息；
- `allow_interruptions=False` 保证开场白不被考生抢话打断。

### 为什么不需要 `asyncio.sleep(1)`

旧代码在 `generate_reply` 前睡 1 秒，猜测“音频就绪”。`session.say` 会由会话调度器排队，等音频输出就绪后播放，不需要猜。

---

## 6.7 命令行入口

```python
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        logger.warning(
            "You started the agent with `dev`. This only registers a worker and waits for a job assignment. "
            "For local room testing, use `python agent.py connect --room ai-room` ..."
        )
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
```

`cli.run_app` 解析参数：

| 命令 | 行为 |
| --- | --- |
| `python agent.py dev` | 注册 worker，等待 job；本地单房间不推荐 |
| `python agent.py connect --room ai-room` | 直接进房，本地调试推荐 |
| `python agent.py start` | 生产 worker 模式 |

`dev` 的 warning 是刻意加的：很多人第一次跑 `dev` 发现考官不说话，以为代码坏了。

---

## 6.8 函数索引

| 函数/类 | 作用 |
| --- | --- |
| `_safe_identity` | 把任意 identity 规范成 `[0-9A-Za-z_-]`，空则生成 candidate-xxxx |
| `SESSION_ID_RE` | session_id 合法字符集 |
| `_participant_metadata` | 安全解析 participant.metadata |
| `_resolve_session_id` | 读一次性会话 id |
| `_resolve_interview_mode` | 读 quick/real，坏值回退 |
| `SiliconFlowTTS` | TTS 文本清洗 |
| `_build_session` | 组装 VAD/STT/LLM/TTS |
| `entrypoint` | 房间生命周期入口 |
| `publish_event` | 数据通道发送 |
| `main`（`__main__`） | CLI 分发 |

---

## 6.9 常见坑

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `dev` 启动后没反应 | 只注册 worker，没有 job | 本地用 `connect --room ai-room` |
| 考官不说话 | STT/TTS Key 无效、麦克风未授权、房间不对 | 看 agent 日志；先跑 `?demo=1` 排除前端 |
| 考生说一句，考官回两句 | 预生成或重复消费 | 确认 `preemptive_generation=False` |
| 重新进考场直接“面试已结束” | thread_id 复用了旧会话 | 确认 token 带 `session_id`，服务端已更新 |
| 中文日志乱码 | 控制台编码 | Bash 里 `chcp 65001` 或看日志文件 |
| TTS 声音断断续续 | 网络/采样率 | 换 `TTS_RESPONSE_FORMAT`，检查网络 |

---

## 6.10 检查点

1. `dev` 和 `connect` 的区别是什么？为什么本地推荐后者？
2. 为什么 `thread_id` 优先用 token 里的 `session_id`？
3. `preemptive_generation=True` 会带来什么具体问题？
4. `session.say` 和 `generate_reply` 在语义上有什么不同？
5. 数据通道发送失败时，为什么不让整个面试失败？

下一章：浏览器端如何把这些都呈现出来。

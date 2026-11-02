# 第 5 章：把 LangGraph 伪装成 LLM（langgraph_adapter.py）

LiveKit 的 `AgentSession` 只认识 `llm.LLM` 接口。LangGraph 是一个状态图，不是 LLM。两者之间的桥就是 `LangGraphLLM`。

这一章讲清楚三件事：

1. LiveKit 期望的接口长什么样；
2. 怎么把一次图调用翻译成 chunk 流；
3. 怎么把图的状态翻译成 UI 事件，同时避免重复发送。

---

## 5.1 LiveKit 的 LLM 接口

简化后的契约：

```python
class LLM:
    def chat(self, *, chat_ctx, tools=None, conn_options=..., **kwargs) -> LLMStream: ...
    async def aclose(self): ...

class LLMStream:
    async def _run(self): ...   # 后台任务，往 self._event_ch 里写 ChatChunk
```

`AgentSession` 会：

1. 把当前对话上下文（`ChatContext`）传给你的 `chat()`；
2. 后台运行返回的 `LLMStream`；
3. 从 `stream` 里迭代 `ChatChunk`，把文本交给 TTS；
4. 调用 `aclose()` 清理。

所以适配器要做的不是“返回一个字符串”，而是**返回一个会异步产出 chunk 的对象**。

---

## 5.2 `LangGraphLLM.__init__`

```python
class LangGraphLLM(llm.LLM):
    def __init__(self, thread_id=None, on_event=None, interview_mode=None):
        super().__init__()
        self.thread_id = thread_id or str(uuid.uuid4())
        self.graph = interview_graph
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self.api_lock = asyncio.Lock()
        self._on_event = on_event
        self.interview_mode = normalize_interview_mode(interview_mode or config.interview_mode)
        self._last_phase = None
        self._report_emitted = False
```

字段解释：

| 字段 | 作用 |
| --- | --- |
| `thread_id` | LangGraph checkpoint 的 key；来自 token `session_id` |
| `graph` | 全局编译好的图；测试里可替换成 `FakeGraph` |
| `config` | 每次 `astream` 都带上的 thread 配置 |
| `api_lock` | 同一会话的图调用串行化，避免两次调用同时推进 state |
| `_on_event` | 发 UI 事件的回调（agent.py 里是 `publish_data`） |
| `interview_mode` | quick/real，每轮传给图 |
| `_last_phase` | 只发一次阶段事件 |
| `_report_emitted` | 成绩单只发一次 |

### 为什么需要 `api_lock`

正常语音回合是串行的，但异常/打断/重试可能让两个 `_run` 重叠。图调用有副作用（推进题号、写记录），并发执行会破坏状态。`async with graph_llm.api_lock:` 把同一会话的图调用排队。

### 为什么是 per-instance 而不是全局锁

锁的粒度应该是“一场面试”。全局锁会让所有考生互相等待；每场一个 `LangGraphLLM` 实例，锁天然按会话隔离。

---

## 5.3 `chat()`：从 `ChatContext` 取考生的话

```python
def chat(self, *, chat_ctx, tools=None, conn_options=..., **kwargs) -> LangGraphLLMStream:
    del tools, parallel_tool_calls, tool_choice, extra_kwargs
    query = self._extract_query(chat_ctx)
    return LangGraphLLMStream(
        llm_instance=self,
        chat_ctx=chat_ctx,
        tools=[],
        conn_options=conn_options,
        query=query,
    )
```

这个项目不使用工具调用，所以把 `tools` 等参数丢掉。真正的消息提取在 `_extract_query`。

### `_extract_query`

```python
def _extract_query(self, chat_ctx) -> str:
    for msg in reversed(chat_ctx.messages()):
        if msg.role != "user":
            continue
        if msg.text_content:
            return msg.text_content.strip()
        parts = [p.strip() for p in msg.content if isinstance(p, str) and p.strip()]
        if parts:
            return "\n".join(parts).strip()
    return ""
```

规则：

- 从最新消息往前找；
- 只认 `role == "user"`；
- 支持 `text_content` 和多段字符串内容；
- **找不到就返回空字符串，绝不回退到 assistant 消息**。

为什么不能回退到 assistant：图把输入当作“考生回答”。如果把上一句考官问题当成回答喂进去，状态机会在不该推进的时候推进。历史版本有这个回退，是一个潜在的状态污染源。现在 `_run` 遇到空 query 直接跳过图。

---

## 5.4 `LangGraphLLMStream._run`：核心翻译

```python
async def _run(self):
    if not self._query:
        logger.info("LangGraphLLMStream: no user utterance, skipping graph")
        return

    graph_llm = self._graph_llm
    await graph_llm.emit_event({"type": "status", "state": "thinking"})

    async with graph_llm.api_lock:
        streamed_any = False
        result = {}
        try:
            input_state = {
                "user_query": self._query,
                "interview_mode": graph_llm.interview_mode,
            }
            async for mode, chunk in graph_llm.graph.astream(
                input_state,
                config=graph_llm.config,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    if isinstance(chunk, dict) and chunk.get("type") == "delta":
                        text = chunk.get("text") or ""
                        if text:
                            streamed_any = True
                            self._send_chunk(text)
                elif mode == "values" and isinstance(chunk, dict):
                    result = chunk

            reply_text = str(result.get("reply") or "好的，请继续。")

            if not streamed_any:
                self._send_chunk(reply_text)

            phase = result.get("current_phase")
            if graph_llm.should_emit_phase(phase):
                await graph_llm.emit_event({"type": "phase", "phase": phase})

            report_data = result.get("report_data")
            if isinstance(report_data, dict) and report_data and graph_llm.should_emit_report():
                await graph_llm.emit_event({
                    "type": "report",
                    "report": report_data,
                    "text": result.get("final_report", ""),
                })

            await graph_llm.emit_event({"type": "status", "state": "speaking"})
        except Exception as e:
            logger.error(...)
            await graph_llm.emit_event({"type": "status", "state": "listening"})
            self._send_chunk("系统处理出现异常，请稍后再试。")
```

逐段解释。

### 空 query 提前返回

没有考生话语，就没有“一轮”。不调用图，也不发 chunk。TTS 不会说话，状态机不会推进。

### `status=thinking`

在进入图之前发，让前端把 AI 头像显示成“思考中”。这是纯 UI 事件，不影响音频。

### 两个 stream mode

```python
stream_mode=["custom", "values"]
```

- `custom`：节点里通过 `get_stream_writer()` 写的任意数据。这里只用 `{"type":"delta","text":...}`。
- `values`：每次状态更新后的完整 state。最后一个 `values` 就是本轮最终结果。

为什么不用 `messages` 模式：本项目的节点内部可能调用多个 LLM（判分、出题、追问），`messages` 流会把所有模型的 token 混在一起。`custom` 让节点自己决定“哪些 token 是要说给考生听的”。

### `streamed_any` 与静态台词

不是所有回复都来自 token 流：

- 自我介绍合格后的过渡语是固定字符串；
- 追问次数用尽后的过渡语是固定字符串；
- 报告失败时的兜底语是固定字符串。

这些没有 `custom` delta。如果直接依赖流，TTS 会一句话都不说。所以：

```python
if not streamed_any:
    self._send_chunk(reply_text)
```

如果已经有 token 流，就不要再发整段，否则同一句话会被说两遍。

### `_send_chunk`

```python
def _send_chunk(self, text: str) -> None:
    self._event_ch.send_nowait(
        llm.ChatChunk(
            id=self._request_id,
            delta=llm.ChoiceDelta(role="assistant", content=text),
        )
    )
```

`_event_ch` 是 `LLMStream` 基类提供的队列，`AgentSession` 从里面读。`send_nowait` 是非阻塞的；流对象的生命周期由基类管理。

### 阶段事件只发一次

```python
if graph_llm.should_emit_phase(phase):
    await graph_llm.emit_event({"type": "phase", "phase": phase})
```

为什么去重：

- 图每轮都会返回 `current_phase`，即使没变；
- 前端虽然会忽略重复，但日志和网络都没必要。

阶段真正变化时（self_intro → pro_qa → compre_qa → completed）才发。

### 成绩单只发一次

这是本轮架构修复的重点之一。

LangGraph 的 checkpoint 会保留 `report_data`。面试结束后的下一轮，`node_completed` 不会清空它，所以 `result.get("report_data")` 仍然非空。如果适配器每次都发 `report` 事件，前端会每次说话都重新弹出成绩单。

```python
def should_emit_report(self) -> bool:
    if self._report_emitted:
        return False
    self._report_emitted = True
    return True
```

一个适配器实例代表一场面试，所以“只发一次”正好。

另一个细节：报告 JSON 解析失败时，`report_data` 是空对象，但 `final_report` 里有原始文本。适配器只要看到任一非空就发 `report`，前端 `handleReportMessage` 会在结构化数据为空时回退到 raw 文本。这样“报告生成失败”也不会变成“什么都没有”。

### 异常处理

```python
except Exception as e:
    logger.error("[LangGraphLLMStream] Error executing graph: %s", e, exc_info=True)
    await graph_llm.emit_event({"type": "status", "state": "listening"})
    self._send_chunk("系统处理出现异常，请稍后再试。")
```

设计取舍：

- 不让异常冒泡到 AgentSession，否则整个语音会话可能崩；
- 给考生一句可理解的兜底；
- 完整堆栈进日志，方便排查；
- 状态回到 `listening`，让 UI 不一直显示“思考中”。

更细的做法是区分可重试/不可重试错误（超时 vs 401），当前版本交给 `ChatOpenAI` 的 `max_retries` 处理连接类错误，最终仍失败才走这里。

---

## 5.5 事件协议

适配器发出、agent 转发、前端消费的事件：

| type | 载荷 | 何时发 | 前端行为 |
| --- | --- | --- | --- |
| `status` | `{"state":"thinking"\|"listening"\|"speaking"}` | 图调用前/后 | AI 头像状态 |
| `phase` | `{"phase":"self_intro"\|"pro_qa"\|"compre_qa"\|"completed"}` | 阶段首次出现 | 阶段条/标题 |
| `report` | `{"report":{...},"text":"..."}` | 成绩单生成时，仅一次 | 弹成绩单 |

`agent.py` 负责把事件序列化成 UTF-8 JSON，通过 LiveKit data channel 发送，topic=`interview`。

---

## 5.6 测试适配器

`tests/test_langgraph_adapter.py` 用 `FakeGraph` 替代真实图：

```python
class FakeGraph:
    def __init__(self, result, custom_chunks=None):
        self.result = result
        self.custom_chunks = custom_chunks or []
        self.calls = []

    async def astream(self, input_state, config=None, stream_mode=None):
        self.calls.append({...})
        for chunk in self.custom_chunks:
            yield ("custom", chunk)
        yield ("values", dict(self.result))
```

这样可以验证：

- 传进去的 `user_query`、`interview_mode`、`thread_id` 对不对；
- custom delta 是否只发一次（不重复整段）；
- `phase` / `report` 事件的契约；
- 坏的事件 sink 不会破坏回复；
- 只有 assistant 消息时不调用图；
- 连续两轮不会重复发 report。

读 chunk 的辅助函数：

```python
async def _read_reply(stream):
    parts = []
    async for chunk in stream:
        content = getattr(getattr(chunk, "delta", None), "content", None)
        if content:
            parts.append(content)
    return "".join(parts)
```

它真实地驱动 `LLMStream` 的后台任务，而不是只调 `_run()`，所以能覆盖队列/生命周期问题。

---

## 5.7 函数索引

| 成员 | 作用 |
| --- | --- |
| `LangGraphLLM.__init__` | 保存 thread/mode/事件回调/锁 |
| `emit_event` | 尽力发事件；sink 异常只记日志 |
| `should_emit_phase` | 阶段首次出现才发 |
| `should_emit_report` | 成绩单只发一次 |
| `model` / `provider` | LiveKit 展示用的元信息 |
| `chat` | 构造流 |
| `aclose` | 无资源需要清理 |
| `_extract_query` | 从 chat_ctx 取最新考生话语 |
| `LangGraphLLMStream._run` | 调图、转发 token、发事件、兜底 |
| `_send_chunk` | 往事件队列写一个 ChatChunk |

---

## 5.8 常见坑

| 坑 | 表现 | 原因 | 修法 |
| --- | --- | --- | --- |
| 把 assistant 消息当 query | 状态错乱 | 回退逻辑 | 只认 user，空则跳过 |
| 有 token 流又发整段 | 同一句说两遍 | 忘记 `streamed_any` | 有 delta 就不发整段 |
| 每轮都发 report | 成绩单反复弹 | checkpoint 保留 `report_data` | 实例级 `_report_emitted` |
| 并发图调用 | 题号错乱 | 两个 `_run` 重叠 | `api_lock` |
| 异常冒泡 | 会话崩溃 | 没 catch | 兜底台词 + 日志 |
| `_send_chunk` 在 `_run` 外调用 | 队列时序错 | 生命周期 | 只在 `_run` 内发 |

---

## 5.9 检查点

1. 为什么 `custom` 流只转发 `delta`，而不是所有自定义数据？
2. 如果一轮回复既有 token 流又是静态台词，会发生什么？代码如何避免？
3. `should_emit_report` 为什么放在 `LangGraphLLM` 实例上，而不是图里？
4. 只有 assistant 消息时，适配器为什么不说话？
5. 怎样用 `FakeGraph` 验证 `thread_id` 确实传给了图？

下一章：把适配器放进真正的语音会话。

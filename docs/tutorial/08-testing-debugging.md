# 第 8 章：测试与调试

这个项目最大的优势之一是：**不接网络、不接 Key、不接 LiveKit，也能验证核心逻辑**。当前离线测试 148 项，覆盖状态机、适配器、Agent 装配、HTTP 服务与配置预检。

---

## 8.1 测试分层

```text
tests/
├── test_graph_structure.py    # 路由表 + 图能编译
├── test_graph_nodes.py        # 单个节点的输入/输出
├── test_graph_flow.py         # 假 LLM 驱动完整多轮
├── test_langgraph_adapter.py  # 适配器事件契约 + 流式
├── test_agent_stream.py       # Agent 装配 + metadata 解析
└── test_server.py             # 真实 HTTP 服务器（端口 0）
```

| 层 | 替身 | 验证什么 |
| --- | --- | --- |
| 节点 | `MagicMock` + `AsyncMock` | 分支、返回值、阈值 |
| 图 | `FakeListChatModel` / mock | 阶段推进、题量、报告时机 |
| 适配器 | `FakeGraph` | 事件、token 流、去重 |
| Agent | `monkeypatch` + fake session | metadata、thread_id、开场 |
| HTTP | 真 `ThreadingHTTPServer` | 路由、安全、token |

---

## 8.2 运行测试

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

只跑某一层：

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph_flow.py -q
```

只跑一个用例：

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph_nodes.py::test_pro_qa_followup -q
```

显示日志：

```bash
.venv/Scripts/python.exe -m pytest tests/ -q -s --log-cli-level=INFO
```

> Windows 上直接用 `pytest` 可能调用到系统 Python，导致 `ModuleNotFoundError: livekit`。用 `.venv/Scripts/python.exe -m pytest` 最稳。

---

## 8.3 假 LLM：按提示词返回固定答案

`test_graph_flow.py` 的关键：

```python
def _fake_llm_response(messages):
    system_prompt = messages[0].content
    replies = {
        INTRO_JUDGE_SYSTEM: "合格",
        PRO_QUESTION_SYSTEM: "请解释进程与线程的区别。",
        PRO_JUDGE_SYSTEM: "不需要",
        TRANSITION_TO_COMPRE_SYSTEM: "专业课问答结束，接下来聊聊你的综合经历。",
        COMPRE_QUESTION_SYSTEM: "请讲一次你在团队合作中遇到的冲突。",
        COMPRE_JUDGE_SYSTEM: "不需要",
        FINAL_REPORT_SYSTEM: REPORT_JSON,
    }
    if system_prompt not in replies:
        raise AssertionError(f"Unexpected prompt: {system_prompt[:40]}")
    return AIMessage(content=replies[system_prompt])
```

为什么按系统提示词匹配，而不是按调用顺序：

- 节点内部调用顺序可能变；
- 新增一个 LLM 调用时，顺序匹配会静默错位；
- 按提示词匹配能明确“谁被调用了”。

`raise AssertionError` 是故意的：如果节点发出了没登记的提示词，测试立刻失败，而不是返回一个模糊的默认值。

---

## 8.4 假图：验证适配器

```python
class FakeGraph:
    def __init__(self, result, custom_chunks=None):
        self.result = result
        self.custom_chunks = custom_chunks or []
        self.calls = []

    async def astream(self, input_state, config=None, stream_mode=None):
        self.calls.append({"input": input_state, "config": config, "stream_mode": stream_mode})
        for chunk in self.custom_chunks:
            yield ("custom", chunk)
        yield ("values", dict(self.result))
```

`adapter.graph = FakeGraph(...)` 替换真实图。这样可以断言：

```python
assert adapter.graph.calls[0]["input"]["user_query"] == "好的老师"
assert adapter.graph.calls[0]["input"]["interview_mode"] == "quick"
assert adapter.graph.calls[0]["stream_mode"] == ["custom", "values"]
```

### 读流

```python
async def _read_reply(stream):
    parts = []
    async for chunk in stream:
        content = getattr(getattr(chunk, "delta", None), "content", None)
        if content:
            parts.append(content)
    return "".join(parts)
```

它真实驱动 `LLMStream` 的后台任务和队列，所以能发现“`_run` 逻辑对，但队列时序不对”的问题。

---

## 8.5 假 AgentSession

`test_agent_stream.py` 用最小替身：

```python
class _FakeSession:
    def __init__(self):
        self.started = []
        self.generated = []
        self.said = []

    async def start(self, **kwargs): self.started.append(kwargs)
    def generate_reply(self, **kwargs): self.generated.append(kwargs)
    def say(self, text, **kwargs): self.said.append({"text": text, **kwargs})
```

验证：

- `session.start` 收到 room；
- 开场用 `say(config.greeting_text)`，不再走 `generate_reply`；
- metadata 里的 `session_id` 传给 `_build_session`；
- 旧 token 没有 `session_id` 时回退到 `房间:identity`。

`_build_session` 需要 `silero.VAD.load`，所以测试里 monkeypatch：

```python
monkeypatch.setattr("agent.silero.VAD.load", lambda **kwargs: object())
```

---

## 8.6 HTTP 测试

`test_server.py` 不 mock 服务器，而是：

```python
httpd = server.create_server(host="127.0.0.1", port=0)
thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
thread.start()
yield httpd.server_address[1]
httpd.shutdown(); httpd.server_close(); thread.join(5)
```

`port=0` 让系统分配空闲端口，避免测试之间抢 8080。

`frontend_root` fixture 建一个临时目录，并放一个“名字以 frontend 开头的兄弟目录”：

```python
evil_sibling = tmp_path / "frontend_evil"
evil_sibling.mkdir()
(evil_sibling / "secret.txt").write_bytes(SECRET_MARKER)
```

这是专门用来测路径穿越的：旧的 `startswith` 校验会误放行。

token 测试直接解 JWT（不验签，因为测试只需要 claims）：

```python
def decode_claims(token):
    return jwt.decode(token, options={"verify_signature": False})
```

`InsecureKeyLengthWarning` 是预期告警：测试用的是 `devkey/secret` 6 字节密钥，不是生产配置。

---

## 8.7 调试前端

### `?debug=1`

正常页面不会暴露内部控制函数。加 `?debug=1` 后：

```js
window.__interviewDebug = {
  data: handleDataMessage,
  transcription: handleTranscription,
  setLocalIdentity(identity) { ... },
  setPhase, setStatus, setConn,
  openReport, closeReport, toast
};
```

可以在控制台手工模拟后端事件：

```js
window.__interviewDebug.setLocalIdentity('candidate-1');
window.__interviewDebug.data({ type: 'status', state: 'thinking' });
window.__interviewDebug.setPhase('pro_qa');
window.__interviewDebug.data({
  type: 'report',
  report: {
    total_score: 88,
    scores: { profession_know: 86, exp_logic: 90, comper_quality: 89 },
    core_weakness: '例子偏少',
    improvement_tips: '补充细节',
    closing: '感谢参加'
  }
});
```

这比等真实面试跑完快得多。

### 演示模式

```text
http://localhost:8080/?demo=1
http://localhost:8080/?demo=1&mode=real
http://localhost:8080/?demo=1&scene=report
```

演示模式完全本地，不依赖后端 token、不依赖 LiveKit。适合：

- 录屏；
- 改 CSS 后看效果；
- 验证阶段条/成绩单/转写的 UI 行为。

### 浏览器控制台

`console.info('[ai-interviewer] frontend ready', ...)` 在启动时打印。常见错误也会 `console.warn`：

- 数据包不是合法 JSON；
- SDK 加载失败；
- 音频挂载失败。

---

## 8.8 调试音频链路

按“从外到内”排查：

```text
1. 浏览器有没有麦克风权限？
2. room.connect 成功了吗？（顶部状态徽章）
3. room.localParticipant.setMicrophoneEnabled(true) 成功了吗？
4. LiveKit 房间里有没有 agent participant？
5. agent 日志有没有 "Participant joined"？
6. STT 有没有返回文本？（转写区出现考生行）
7. 图有没有被调用？（agent 日志 "[LangGraph] Processing..."）
8. TTS 有没有音频轨道？（浏览器 <audio> 元素 / 声音）
```

每一层的检查点：

| 层 | 看哪里 |
| --- | --- |
| 浏览器 | DevTools Console / Network / 麦克风图标 |
| 前端状态 | 顶部状态徽章、错误卡 |
| LiveKit | `livekit-server --dev` 日志、房间 participant 列表 |
| Agent | `python agent.py connect` 控制台日志 |
| STT/TTS | 转写区、是否有声音 |
| LLM | agent 日志里的 reply 前 60 字、`/api/health` 不检查 LLM |

---

## 8.9 常见故障速查

| 现象 | 最可能的原因 | 快速验证 |
| --- | --- | --- |
| `ModuleNotFoundError: livekit` | 用错 Python | `.venv/Scripts/python.exe -m pytest` |
| `Port 8080 is already in use` | 另一个 server 在跑 | `netstat -ano \| findstr :8080` |
| 页面 404 | 没在项目根目录启动 | `python server.py` 的 cwd |
| token 400 invalid mode | 传了 `turbo` 之类 | 用 quick/real |
| token 403/404 | 静态路径穿越/文件不存在 | 看 URL 是否有多余 `..` |
| agent 不说话 | `dev` 只注册 worker | 用 `connect --room ai-room` |
| 考官重复上一句 | 某节点没返回 `reply` | 看 `test_graph_flow` 是否过 |
| 成绩单反复弹 | 适配器去重失效 | 看 `should_emit_report` 测试 |
| 重新进考场直接结束 | 复用旧 thread | token metadata 有没有 `session_id` |
| 中文日志崩 | cp936 控制台 | 本项目日志已容错；仍建议 `chcp 65001` |

---

## 8.10 检查点

1. 为什么假 LLM 要按系统提示词匹配，而不是按调用顺序？
2. `FakeGraph` 的 `astream` 为什么要先 yield custom 再 yield values？
3. HTTP 测试为什么用 `port=0`？
4. `?debug=1` 暴露了哪些能力？生产环境为什么默认关闭？
5. 如果“考生说话后 AI 不回复”，你会按什么顺序排查？

下一章：把以上所有模块按里程碑重新手搓一遍。

# 第 9 章：从空目录手搓一遍

这一章把前面所有内容压缩成一条可执行的建造路线。每个里程碑都有：

- **目标**：这一步结束，什么应该为真；
- **动手**：写哪些文件/函数；
- **命令**：怎么验证；
- **检查点**：如果失败，先查什么。

建议严格按顺序。不要一开始就写完整状态机；先让“能连上、能说话、能推进一轮”跑通。

---

## 里程碑 0：先定契约

在写代码前，把三个跨层契约写在一张纸上：

### 状态字段

```text
current_phase: self_intro | pro_qa | compre_qa | report | completed
user_query, reply
question_pro, question_compre
is_answering, follow_up_count, questions_asked
interview_mode, questions_per_phase, max_follow_ups, max_intro_attempts
asked_questions, raw_scores
final_report, report_data
```

### 数据通道事件

```json
{"type":"status","state":"thinking|listening|speaking"}
{"type":"phase","phase":"self_intro|pro_qa|compre_qa|completed"}
{"type":"report","report":{...},"text":"..."}
{"type":"mode","mode":"quick|real"}
```

### token metadata

```json
{"interview_mode":"quick","session_id":"<server-generated>"}
```

这三个契约定错，后面每一层都要返工。尤其是 `session_id`：它决定了 checkpoint 的隔离边界。

---

## 里程碑 1：环境能导入

### 目标

`python -c "import livekit, langgraph, langchain_openai"` 不报错。

### 动手

```bash
mkdir ai-interviewer && cd ai-interviewer
python -m venv .venv
source .venv/Scripts/activate
```

`requirements.txt` 写入第 2 章的依赖，然后：

```bash
pip install -r requirements.txt
```

### 命令

```bash
.venv/Scripts/python.exe -c "import livekit, langgraph, langchain_openai; print('ok')"
```

### 检查点

- 如果 `livekit` 导入失败，确认你用的是虚拟环境里的 Python。
- 如果 `langgraph` 版本太旧，`stream_mode=["custom","values"]` 可能不支持；按 requirements 安装。

---

## 里程碑 2：配置与模式

### 目标

`from config import config` 能打印出 base_url/model；`from interview_modes import normalize_mode` 能返回 quick/real。

### 动手

1. 写 `config.py`（第 2 章）。
2. 写 `interview_modes.py`。
3. 复制 `.env.example` 为 `.env`，填 Key。

### 命令

```bash
.venv/Scripts/python.exe -c "from config import config; print(config.llm_model, bool(config.llm_api_key))"
```

```bash
.venv/Scripts/python.exe -c "from interview_modes import normalize_mode; print(normalize_mode('REAL'), normalize_mode('bogus'))"
```

期望：

```text
Qwen/Qwen2.5-7B-Instruct True
real quick
```

### 检查点

- `bool(...)` 为 False：`.env` 不在 cwd 或字段名写错。
- 不要在这一步就调真实 LLM；后面用测试替身先跑通逻辑。

---

## 里程碑 3：最小 HTTP 服务

### 目标

`GET /api/health` 返回 `{"status":"ok"}`，`GET /` 返回 `frontend/index.html`。

### 动手

1. 写 `server.py` 的 `configure_logging`、`InterviewHttpHandler`、`_handle_static`、`run_server`。
2. 建 `frontend/index.html`，先只写一行 `<h1>AI Interviewer</h1>`。

### 命令

```bash
python server.py
```

另开终端：

```bash
curl -i http://localhost:8080/api/health
curl -i http://localhost:8080/
```

### 检查点

- 404：`FRONTEND_DIR` 计算错，或文件不在 `frontend/`。
- 端口占用：`FRONTEND_PORT=8090 python server.py`。
- 中文 Windows 启动崩：确认没有 `print("🚀")`，日志走 logging。

---

## 里程碑 4：Token 端点

### 目标

`GET /api/token?mode=real` 返回五个字段，token metadata 里有 `interview_mode` 和 `session_id`。

### 动手

1. 写 `create_token`：`AccessToken` + `VideoGrants(room_join=True, room=...)` + metadata + TTL。
2. 写 `_handle_token`：参数校验、mode 校验。
3. 写 `_resolve_static_path` 的路径穿越防护。

### 命令

```bash
curl -s "http://localhost:8080/api/token?mode=real" | python -m json.tool
```

```bash
curl -i "http://localhost:8080/api/token?mode=turbo"
```

```bash
curl -i "http://localhost:8080/%2e%2e%2fREADME.md"
```

### 期望

- token 200，含 `serverUrl/roomName/identity/token/interviewMode`；
- `turbo` 400；
- 路径穿越 403。

### 检查点

- metadata 里没有 `session_id`：`create_token` 没加，或用了旧版本。
- token TTL 是 6 小时：没调 `.with_ttl(...)`。
- 任意网站能读 token：CORS 配置错了，默认不应发 `Access-Control-Allow-Origin`。

---

## 里程碑 5：最小状态机（只做自我介绍）

### 目标

用假 LLM 跑通：

```text
输入一段合格自我介绍 → current_phase == "pro_qa"
输入一段太短的自我介绍 → current_phase == "self_intro"
```

### 动手

1. 定义 `InterviewState`。
2. 写 `node_self_intro`。
3. 写 `route_phase`。
4. 写 `create_interview_graph`，只加 `self_intro` 节点。
5. 写 `tests/test_graph_structure.py`。

### 命令

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph_structure.py -q
```

### 检查点

- 图编译失败：`StateGraph(InterviewState)` 的字段名拼错。
- 第二轮输入丢失：没有传 `config={"configurable":{"thread_id":...}}`。
- 太短仍合格：代码阈值没生效，或被模型输出覆盖。

---

## 里程碑 6：完整图与成绩单

### 目标

用假 LLM 跑完整 quick/real 两种模式，最后一题答完同轮出报告。

### 动手

按第 4 章顺序加：

1. `node_pro_qa`（出题/追问/下一题/过渡）
2. `node_compre_qa`（出题/追问/报告）
3. `_generate_report` / `_build_report_payload`
4. `node_completed`
5. `route_phase` 的 `report`/`end` 分支
6. `tests/test_graph_flow.py`

### 命令

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph_flow.py -q
```

### 期望

- quick：自我介绍 → 1 道专业 → 1 道综合 → 报告；
- real：自我介绍 → 3 道专业 → 3 道综合 → 报告；
- 报告在最后一题答完的同一轮出现；
- 结束后再说话 → “面试已经结束了”。

### 检查点

- 报告被推迟一轮：`node_compre_qa` 的题量用尽分支返回了 `current_phase="report"`，应直接 `_generate_report`。
- 重复播上一题：某分支没返回 `reply`。
- 题目重复：没加 `asked_questions`。
- 报告缺分项：`_build_report_payload` 的默认值被误删。

---

## 里程碑 7：适配器

### 目标

`LangGraphLLM(thread_id="t").chat(chat_ctx=...)` 能产出 chunk；custom delta 不被重复整段发送；事件顺序正确。

### 动手

1. 写 `LangGraphLLM`：`chat`、`_extract_query`、`emit_event`。
2. 写 `LangGraphLLMStream._run`。
3. 写 `_send_chunk`。
4. 写 `FakeGraph` 测试。

### 命令

```bash
.venv/Scripts/python.exe -m pytest tests/test_langgraph_adapter.py -q
```

### 期望

- `status=thinking` → reply chunks → `phase` → `status=speaking`；
- 有 custom delta 时，最终 reply 不重复；
- 没有 user 消息时不调用图；
- 连续两轮 report 只发一次。

### 检查点

- TTS 说两遍：`streamed_any` 逻辑没生效。
- 状态机被 assistant 消息推进：`_extract_query` 回退了 assistant，删掉。
- 成绩单反复弹：没做 `_report_emitted`。

---

## 里程碑 8：Agent 会话与开场

### 目标

`python agent.py connect --room ai-room` 启动后，考生进房能听到开场白。

### 动手

1. 写 `_build_session`：VAD + STT + LangGraphLLM + TTS。
2. 写 `entrypoint`：connect、wait_for_participant、metadata、thread_id。
3. 写 `publish_event`。
4. 开场用 `session.say(config.greeting_text)`。

### 命令

```bash
# 终端 1
livekit-server --dev
```

```bash
# 终端 2
python server.py
```

```bash
# 终端 3
python agent.py connect --room ai-room
```

浏览器进考场。

### 期望

- 浏览器听到开场白；
- agent 日志有 `Participant joined ... mode=... thread=...`；
- 浏览器顶部状态从“连接中”变“面试进行中”。

### 检查点

- 没有声音：TTS Key/voice；`session.say` 前会话是否已 start。
- 开场白是“请先向考生打招呼……”：用了旧 `generate_reply`，改成 `say`。
- 重新进考场直接结束：thread_id 没用 `session_id`。

---

## 里程碑 9：数据通道与前端实时状态

### 目标

阶段条、AI 状态、转写、成绩单能随面试更新。

### 动手

1. agent 的 `publish_event` 用 `topic="interview"`。
2. 前端 `wireRoom` 监听 `DataReceived`、`TranscriptionReceived`。
3. 写 `handleDataPacket` / `handleDataMessage`。
4. 写 `handleTranscription` / `upsertSegment`。
5. 写 `setPhase` / `setAiStatus`。
6. 写成绩单 `handleReportMessage` / `openReport`。

### 命令

```bash
python server.py
```

浏览器加 `?debug=1`，控制台：

```js
window.__interviewDebug.setPhase('pro_qa');
window.__interviewDebug.data({ type: 'status', state: 'thinking' });
```

### 期望

- 阶段条高亮；
- AI 头像三点动画；
- 转写区考生/考官分栏；
- 成绩单弹窗。

### 检查点

- 阶段不动：后端 `phase` 事件没发，或前端 topic 过滤错。
- 转写重复行：segment key 不稳定。
- XSS：任何地方用了 `innerHTML`，改 `textContent`。

---

## 里程碑 10：音频播放与设备

### 目标

考官声音能播放；自动播放被拦截时有横幅；麦克风可静音/恢复。

### 动手

1. `TrackSubscribed` → `attachAudioTrack`。
2. `AudioPlaybackStatusChanged` → 横幅。
3. `room.startAudio()`。
4. `setMicrophoneEnabled` + 错误翻译。
5. `pagehide` 清理。

### 检查点

- 有转写但没声音：自动播放被拦截或音频元素没挂载。
- 退出后还有声音：`cleanupAudio` 没调。
- 麦克风报错文案是英文：`describeMediaError` 没覆盖。

---

## 里程碑 11：演示模式

### 目标

`?demo=1` 在无 LiveKit、无 Key 时完整走完面试并弹成绩单。

### 动手

1. 写 `DEMO_MODE_SCRIPT`（quick/real）。
2. 写 `demoType`（逐字打字）。
3. 写 `initDemoAutoScene`（考官→考生→追问→下一项）。
4. 写 `showDemoReport`。
5. 写 `?demo=1&scene=report|connected`。

### 检查点

- 演示模式仍请求 token：`IS_DEMO` 分支没走。
- 演示模式打不开：`initDemo` 前 `state.demoActive` 没设。
- 切换模式后脚本没变：`DEMO_MODE_SCRIPT[interviewMode]` 用的旧值。

---

## 里程碑 12：测试与加固

### 目标

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

全绿；`node --check frontend/app.js` 通过；HTTP 冒烟全绿。

### 动手

1. 补 `test_server.py`：静态、token、模式、路径穿越、CORS、端口。
2. 补 `test_agent_stream.py`：metadata、thread_id、开场。
3. 补 `test_langgraph_adapter.py`：事件、去重、空 query。
4. 补 `test_graph_nodes.py`：追问、阈值、报告解析、防重复。
5. 跑真实进程冒烟。

### 命令

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

```bash
node --check frontend/app.js
```

```bash
.venv/Scripts/python.exe -m compileall -q agent.py config.py interview_graph.py interview_modes.py langgraph_adapter.py server.py tests
```

### 检查点

- 测试数明显少于 90：某些层没有覆盖。
- 测试依赖网络：用了真实 LLM/STT/TTS，应改成替身。
- 测试互相污染：`thread_id` 重复，换唯一 id。

---

## 最终验收清单

- [ ] `pytest` 全绿，且离线可跑
- [ ] `server.py` 启动无编码错误
- [ ] `GET /api/token?mode=real` metadata 含 mode + session_id
- [ ] 路径穿越返回 403
- [ ] `?demo=1` 完整跑通 quick/real
- [ ] 真实 LiveKit 下能听到开场白、能回答一轮
- [ ] 面试结束同轮出成绩单
- [ ] 结束后再说话不重复弹成绩单
- [ ] 重新进考场是全新面试
- [ ] 前端所有动态文本用 `textContent`

---

## 如果只能记住三件事

1. **一次语音回合 = 一次图调用**。不要在一次调用里连续跑多个阶段。
2. **custom 流给 TTS，values 流给状态/UI**。不要把两者混在一起。
3. **thread_id 必须一场面试一个**。用服务端生成的 `session_id`，不要只用 identity。

下一章：如果要把“只会听文字”升级成“能听语气、看表情”的多模态系统，该怎么做。

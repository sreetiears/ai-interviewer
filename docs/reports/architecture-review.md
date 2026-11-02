# 架构审查与优化报告

- **审查日期**：2026-11-02
- **审查对象**：`ai-interviewer`（分支 `ai_made`，审查前 HEAD `79dda92`）
- **审查范围**：Python 后端（`agent.py`、`config.py`、`interview_graph.py`、`langgraph_adapter.py`、`server.py`）、前端（`frontend/`）、测试与运行配置。
- **结论**：整体分层正确（媒体 / 状态机 / 适配器 / HTTP / 前端），但存在 4 个会实际影响面试正确性的缺陷、5 个可维护性/健壮性缺陷、7 个安全/运维缺口。本轮已修复前两类共 9 项；未修复项在文末列出，均不需要改动核心架构。
- **验证**：`105 passed`（修复前 92），`compileall` 通过，HTTP/token 冒烟通过。

---

## 1. 审查方法

1. 阅读全部核心源码与测试，画出三条流（音频上行、决策、音频下行 + UI）。
2. 用测试驱动完整 quick/real 流程，确认状态机行为。
3. 用真实 `ThreadingHTTPServer`（端口 0）冒烟静态文件、token、路径穿越、模式校验。
4. 对每个候选问题，先写一个能复现的测试或命令，再改代码。
5. 修改后重跑全量测试与冒烟。

---

## 2. 发现的问题

### 2.1 P1：会导致面试状态错误

#### P1-1 会话状态可能串场

**位置**：`agent.py::_build_session` / `entrypoint`

**问题**：`thread_id` 直接使用考生 identity。LangGraph 的 `MemorySaver` 按 `thread_id` 存档。如果同一考生重新进入同一房间（或两个房间复用 identity），会继承上一场的 `current_phase="completed"`、`raw_scores`、`report_data`。

**影响**：

- 重新进考场，考官第一句就是“面试已经结束了”；
- 上一场的记录混进新一场的成绩单；
- 多考生/多房间部署时更难排查。

**修复**：

- `/api/token` 为每个 token 生成一次性 `session_id`，写入 metadata；
- `agent.py` 读取 `session_id` 作为 `thread_id`；
- 旧客户端没有 `session_id` 时回退到 `房间名:identity`。

**验证**：

- `test_agent_stream.py::test_entrypoint_publishes_mode_and_initial_phase`
- `test_agent_stream.py::test_entrypoint_falls_back_to_room_and_identity_for_old_tokens`
- `test_server.py::test_create_token_gets_a_fresh_session_id_per_token`

#### P1-2 开场白依赖字符串匹配

**位置**：`agent.py::entrypoint`、`langgraph_adapter.py::LangGraphLLMStream._run`

**问题**：开场通过 `generate_reply(user_input=config.opening_prompt)` 送进图，适配器用：

```python
if "打招呼" in self._query or "开始自我介绍" in self._query:
```

拦截。只要 `OPENING_PROMPT` 改一个字（或换语言），拦截失效，开场指令会被 `node_self_intro` 当成考生自我介绍处理。

**影响**：开场白不出现，或考生一进房就被要求“补充自我介绍”。

**修复**：

- 新增 `GREETING_TEXT`，语义是“直接说出来的台词”；
- `entrypoint` 用 `session.say(config.greeting_text, allow_interruptions=False)`；
- 删除适配器里的字符串拦截；
- 删除不必要的 `asyncio.sleep(1)`。

**为什么这是架构修复而不是小补丁**：开场白不是 LLM 回合，不应该进入状态机；`say` 是 LiveKit 提供的一等能力，绕过 LLM 是正确边界。

**验证**：

- `test_agent_stream.py::test_entrypoint_publishes_mode_and_initial_phase` 断言 `session.say` 被调用、`generate_reply` 未被调用；
- `test_langgraph_adapter.py` 删除了旧的“开场拦截”测试。

#### P1-3 成绩单事件在结束后重复发送

**位置**：`langgraph_adapter.py::LangGraphLLMStream._run`

**问题**：LangGraph checkpoint 会保留 `report_data`。面试结束后用户再说话，`node_completed` 只返回 `reply/current_phase/is_answering`，`report_data` 仍在 state 里。适配器每次看到非空 `report_data` 就发 `report` 事件。

**影响**：考生每多说一句，成绩单弹窗就重新弹出一次。

**修复**：

- `LangGraphLLM` 增加实例级 `_report_emitted`；
- `should_emit_report()` 保证一场会话只发一次；
- 阶段事件同理增加 `_last_phase`，只在阶段首次出现时发。

**验证**：

- `test_langgraph_adapter.py::test_report_and_phase_are_emitted_once_per_session`

#### P1-4 适配器会把考官上一句当成考生回答

**位置**：`langgraph_adapter.py::LangGraphLLM._extract_query`

**问题**：找不到 user 消息时，回退到最近一条 assistant 消息。图把输入当考生回答，于是考官的上一句会被当成“考生的回答”推进状态机。

**影响**：在空转/异常触发时，题号、追问次数、记录都会错位。

**修复**：

- 只认 `role == "user"`；
- 找不到返回空字符串；
- `_run` 遇到空 query 直接跳过图，不发 chunk、不推进状态。

**验证**：

- `test_langgraph_adapter.py::test_stream_without_user_message_skips_graph`

---

### 2.2 P2：可维护性 / 一致性

#### P2-1 模式常量两处维护

**位置**：`server.py` 与 `interview_graph.py` 各有一份 `("quick","real")`。

**风险**：新增模式时只改一处，出现“token 接受、图不认识”或反之。

**修复**：新增零依赖的 `interview_modes.py`：

```python
MODE_NAMES = ("quick", "real")
DEFAULT_MODE = "quick"
def normalize_mode(value): ...
def is_valid_mode(value): ...
```

`server.py`、`interview_graph.py`、`langgraph_adapter.py`、`agent.py` 统一导入。`server.INTERVIEW_MODES` 保留为别名，避免外部脚本断裂。

#### P2-2 出题没有历史，容易重复

**位置**：`interview_graph.py::node_pro_qa` / `node_compre_qa`

**问题**：real 模式每阶段 3 题，但出题提示词只有“不要重复之前问过的问题”，没有把已问题目给模型。

**修复**：

- state 增加 `asked_questions`；
- 每次主问题/追问追加；
- 下一次出题提示词通过 `_question_prompt` 带上；
- 阶段切换时重置。

**验证**：

- `test_graph_nodes.py::test_next_question_prompt_includes_questions_already_asked`

#### P2-3 每次 LLM 调用新建客户端、无超时

**位置**：`interview_graph.py::_get_llm`

**问题**：每个节点按温度新建 `ChatOpenAI`，`timeout`/`max_retries` 没有统一配置；供应商卡住时可能长时间静默。

**修复**：

- `@lru_cache(maxsize=8)` 按温度复用；
- 统一 `LLM_TIMEOUT_SECONDS`（默认 30）与 `LLM_MAX_RETRIES`（默认 2）。

#### P2-4 Token TTL 使用 LiveKit 默认 6 小时

**问题**：浏览器每次进入考场都会重新取 token，6 小时没有必要。

**修复**：`TOKEN_TTL_SECONDS`（默认 3600），`create_token` 用 `.with_ttl(...)`。

**验证**：

- `test_server.py::test_create_token_ttl_is_bounded`
- 冒烟：`ttl: 3600`

#### P2-5 报告 JSON 解析失败时前端收不到任何报告

**位置**：`langgraph_adapter.py::LangGraphLLMStream._run`

**问题**：`_generate_report` 解析失败时返回 `report_data={}` + `final_report`（原始文本）。适配器只检查 `report_data` 非空才发 `report` 事件，于是浏览器既不弹结构化成绩单，也看不到原始文本，只在语音里听到一句“稍后查看”。

**修复**：只要 `report_data` 非空**或** `final_report` 非空，就发 `report`；结构化为空时前端自动回退到 raw 文本。仍由 `should_emit_report()` 保证只发一次。

**验证**：

- `test_langgraph_adapter.py::test_stream_falls_back_to_raw_report_text`

---

### 2.3 P3：安全 / 运维（未改代码，已在文档中标注）

| 问题 | 现状 | 建议 |
| --- | --- | --- |
| `/api/token` 匿名签发 | 任意来源可拿 token | 公网部署前加登录/房间白名单/限流；`FRONTEND_HOST` 收到 `127.0.0.1` 或反代 |
| `devkey/secret` 默认值 | 仅适合本地 `--dev` | 生产启动时 fail-fast，拒绝默认密钥 |
| 无 token 刷新 | 1 小时后过期 | 长面试可在剩余 < 10 分钟时重新取 token |
| `MemorySaver` 进程内 | 重启/多实例丢失 | 换 Postgres/Redis checkpointer；加 TTL/清理 |
| 无 usage/延迟指标 | 只能看日志 | 记录每轮 LLM tokens、首 token 延迟、TTS 首帧延迟 |
| 前端单文件 1900 行 | 无构建、好部署 | 若继续演进，可拆 ES modules；当前保持零构建是有意取舍 |
| 无真实 LiveKit E2E | 本环境无 `livekit-server` | 在具备服务与 Key 的机器上跑一次完整音频链路 |

---

## 3. 修改清单

| 文件 | 修改 | 目的 |
| --- | --- | --- |
| `interview_modes.py` | **新增** | 模式名/默认值/规范化唯一来源 |
| `config.py` | 新增 `llm_timeout_seconds`、`llm_max_retries`、`greeting_text`；删除 `opening_prompt`；新增 `_env_float` | 超时/重试/开场白配置 |
| `interview_graph.py` | 引入 `interview_modes`；`_get_llm` 加缓存/超时；新增 `asked_questions`、`_question_prompt`、`_remember_question`；各出题/追问分支记录问题；阶段切换重置 | 防重复、统一客户端、去重复常量 |
| `langgraph_adapter.py` | 删除开场字符串拦截；`_extract_query` 只认 user；空 query 跳过图；新增 `should_emit_phase` / `should_emit_report`；报告解析失败时回退 raw 文本 | 状态正确性、去重、报告可见性 |
| `agent.py` | 新增 `_participant_metadata`、`_resolve_session_id`；`_build_session` 接受 `thread_id`；entrypoint 用 `session.say` 开场；删除 `asyncio.sleep` | 会话隔离、稳健开场 |
| `server.py` | 引入 `interview_modes`；`create_token` 加 `session_id`/`ttl`；`TOKEN_TTL_SECONDS`；模式校验用共享常量 | token 契约、安全 |
| `.env.example` | 新增 `LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES`、`TOKEN_TTL_SECONDS`、`GREETING_TEXT`；移除 `OPENING_PROMPT` | 配置可发现 |
| `tests/*` | 更新 metadata 断言；新增 session/thread、空 query、report 去重、防重复、TTL、raw report、模式常量测试 | 回归保护（92 → 105） |

---

## 4. 修改前后对比

### 4.1 会话隔离

```text
修改前：
token metadata = {"interview_mode": "real"}
agent thread_id = identity
同 identity 重进 → 继承 completed

修改后：
token metadata = {"interview_mode": "real", "session_id": "<uuid>"}
agent thread_id = session_id（旧 token 回退 room:identity）
每场面试独立 checkpoint
```

### 4.2 开场

```text
修改前：
entrypoint → generate_reply(user_input="请先向考生打招呼…")
adapter → if "打招呼" in query: 固定台词

修改后：
entrypoint → session.say(config.greeting_text)
adapter 不再拦截任何开场文本
```

### 4.3 成绩单

```text
修改前：每次图返回非空 report_data → emit report
修改后：should_emit_report() 只允许一次
```

### 4.4 出题

```text
修改前：PRO_QUESTION_SYSTEM + "不要重复之前问过的问题"
修改后：同一提示词 + "已经问过的问题：- …"
```

---

## 5. 验证记录

### 5.1 全量测试

```text
$ .venv/Scripts/python.exe -m pytest tests/ -q
105 passed, 17 warnings in 10.01s
```

warning 均为 `devkey/secret` 6 字节 HMAC 密钥的预期告警。生产必须替换。

### 5.2 编译

```text
$ .venv/Scripts/python.exe -m compileall -q agent.py config.py interview_graph.py interview_modes.py langgraph_adapter.py server.py tests
COMPILE_OK
```

### 5.3 HTTP/token 冒烟

```text
GET /api/health           200 application/json
GET /api/token?mode=real  200 application/json
  metadata: {"interview_mode": "real", "session_id": "1c83b5a4b22642609bde42819b2febd1"}
  ttl: 3600
GET /                     200 text/html; charset=utf-8
```

---

## 6. 未应用的方案

以下不是“忘了做”，而是有意留作下一步：

1. **前端拆模块**：当前零构建是部署优势；拆 ES modules 会引入加载顺序/路径/缓存复杂度。若继续加功能，再拆。
2. **持久化 checkpointer**：需要数据库/Redis 与运维；当前进程内内存版足以演示。
3. **token 鉴权/限流**：需要产品层面的登录/邀请码设计，不能由代码单方面决定。
4. **多模态替换**：见 [multimodal-alternatives.md](multimodal-alternatives.md)，只给方案，未改代码。

---

## 7. 结论

本轮修复没有改变系统分层，只修正了边界：

- **会话边界**：`session_id` 让每场面试独立；
- **职责边界**：开场白不再进入状态机；
- **事件边界**：报告/阶段只发一次；
- **输入边界**：只把考生的话当考生的话；
- **常量边界**：模式定义只有一个来源。

修复后，系统在离线测试与 HTTP 冒烟层面是自洽的；真实 LiveKit 音频链路仍需在具备服务与 Key 的环境做一次端到端验收。

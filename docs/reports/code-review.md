# ai-interviewer 代码质量审查报告（HEAD 原状）

- **审查对象**：`HEAD 63c4a0c`（docs: update README with LangGraph architecture and custom frontend guide）
- **审查方式**：全部源码通过 `git show HEAD:<path>` 读取快照，并用 git blob SHA-1 逐文件校验快照与 HEAD 一致（14/14 MATCH），因此下文行号均为 HEAD 文件行号；并行修改中的工作区文件不纳入本报告。
- **审查环境**：Windows 中文控制台（`chcp` = 936 / GBK），Python 3.12.10（`.venv`），依赖版本：livekit-agents 1.5.2、langgraph 1.2.16、langchain-openai 1.7.2、openai 3.36.1、av 19.0.2。
- **复现原则**：只读离线复现，未安装依赖、未调用真实 STT/TTS/LLM API。所有结论均给出 `文件:行号` 与实测命令/输出或可复核的代码路径。

**总体质量评级：C（约 62/100）**。分层意识（config / graph / adapter / server 分离）与基础测试是加分项，但存在“中文 Windows 启动必崩、核心成绩单链路断裂、鉴权与密钥失守”三类硬伤；当前状态适合修完 P0/P1 后做本地演示，不适合直接交付或上线。

**最严重的问题（Top 3）**

1. **`server.py` 在默认中文 Windows 控制台启动即崩溃**（P0，已实测）：第 91 行用 emoji 做启动日志，cp936 下 `UnicodeEncodeError`，README 的主推启动方式 100% 失败。
2. **成绩单/实时转写链路整体缺失**（P1）：`final_report` 只写进 LangGraph state，适配器只把 `reply` 转成语音，后端无任何 `publish_data`，前端也没有数据通道处理；同时每阶段只出 1 道主问题（最多 1 次追问），整场约 5-6 轮即结束。
3. **鉴权与密钥风险**（P1）：`/api/token` 无鉴权 + CORS 通配 + 默认 `devkey/secret`（JWT HMAC 密钥仅 6 字节，pytest 直接告警），且初始提交历史中仍可检出明文 SiliconFlow API Key（当前 HEAD 已移除，历史未清理）。

---

## 1. 结论摘要

### 1.1 分项评价

| 维度 | 评级 | 主要依据 |
|---|---|---|
| 架构设计 | B- | LangGraph 状态图 + LiveKit LLM 适配器的方向正确，但状态机把“出题/评分/追问”混在同一节点，依赖隐式布尔字段推进 |
| 功能完整性 | D+ | 成绩单、实时转写、阶段联动均不可达；面试深度不足（每阶段 1 主问题） |
| 代码质量 | C | 提示词与节点逻辑有大量复制粘贴；存在未使用 import、死代码、`sk-dummy` 静默兜底 |
| 可靠性 | D | 启动必崩、适配器吞异常、无 LLM 超时、完成后重复播报、checkpoint 无限增长 |
| 安全性 | D | token 免鉴权签发 + CORS `*` + 弱默认密钥 + 历史泄密 + 路径前缀校验可绕过 |
| 测试 | D | 8 个 mock 单测，仅覆盖 5 个节点 happy path + 路由表 + token；适配器/agent/HTTP 处理器/综合阶段零覆盖 |
| 文档 | C- | README 声称“无缝流式输出、实时文字记录、成绩单、完整测试”，与实现多处不符 |

### 1.2 一句话结论

这是一个**结构清晰但“演示链路未闭合”的原型**：能跑通“连上房间—听到提问—点退出”的骨架，但“看得见成绩单/转写”的核心产品价值尚未实现，且默认启动方式在目标环境（中文 Windows）不可用。

---

## 2. 分级问题清单

### 2.1 P0（阻断/必崩）

#### P0-1 `server.py` 在 cp936（GBK）控制台下启动即崩溃
- **位置**：`server.py:90-92`
- **问题**：第 91 行 `print(f"🚀 Custom Interview Frontend running at ...")` 含 U+1F680 emoji；Windows 中文控制台默认 GBK，`print` 抛 `UnicodeEncodeError`。
- **证据/影响**：实测 `chcp` 为 936，执行 `FRONTEND_PORT=8093 .venv\Scripts\python.exe server.py` 退出码 1：
  `File ".../server.py", line 91 ... print(f"\U0001f680 ...")` → `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f680'`。
  第 90 行 `HTTPServer(...)` 已完成端口绑定，崩溃发生在“已绑定、未服务”的窗口，第 92 行 `serve_forever()` 从未执行。README:89 的默认启动方式因此完全不可用，前端页面无法加载，属演示阻断。
- **修复建议**：去掉 emoji，或统一用 `logging`（输出到 UTF-8 文件/处理器）；入口处 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；补一个“子进程启动 1 秒内不退出”的 smoke test。

### 2.2 P1（高风险/功能缺失）

#### P1-1 `final_report` 从未送达任何客户端（成绩单功能不存在）
- **位置**：`interview_graph.py:27`、`interview_graph.py:350-354`、`langgraph_adapter.py:110-123`
- **问题**：`node_report` 生成并写入 state 的 `final_report` 只有定义与赋值；适配器只读取 `result["reply"]` 分块发送，后端全仓无 `publish_data`/RPC，前端无 `RoomEvent.DataReceived`。`git grep final_report HEAD -- '*.py'` 只命中定义、赋值与测试。
- **证据/影响**：HEAD 快照上用假 LLM 跑完 6 轮，state 中 `final_report` 存在（含分项分数），但对话侧只收到 `reply`（一句结语）；用户永远看不到成绩单。原 Dify 工作流是把 `final_report` 作为 `answer` 输出的（`interview_word .yml:1996-2000`），LangGraph 重构后该链路丢失。README:18/26 声称输出“多维度数字化成绩单”。
- **修复建议**：在 `node_report` 后由 agent 通过 `ctx.room.local_participant.publish_data(topic="interview.report")` 推送 JSON；前端监听 `DataReceived` 渲染成绩单并激活第 4 步；或提供 `/api/report` 查询。补端到端测试断言“报告可被客户端消费”。

#### P1-2 面试流程过短：每阶段只有 1 道主问题（最多 1 次追问）
- **位置**：`interview_graph.py:193-247`、`interview_graph.py:282-302`
- **问题**：`pro_qa`/`compre_qa` 在考生第一次作答后即进入 judge；judge 不需要追问或追问配额用尽（`max_follow_ups` 默认 1）就立即转阶段并重置 `follow_up_count`。没有 `question_index`/`total_questions` 之类的循环控制。
- **证据/影响**：假 LLM 全流程复现的节点序列为 `self_intro → pro_qa → pro_qa → compre_qa → compre_qa → report → completed`，即整场只有 1 道专业题 + 1 道综合题，约 5-6 轮用户输入即结束（含报告轮）；即使 judge 判“需要追问”，每阶段最多 2 次作答。对“考研复试模拟”而言考察深度严重不足，且无法从配置调整题目数量。
- **修复建议**：state 增加 `question_index`/`questions_per_phase`（或 `asked_questions: Annotated[list[str], operator.add]`），在阶段内循环出题；追问配额只影响当前题目，不影响阶段推进；到达题量后再路由到下一阶段。

#### P1-3 `compre_qa → report` 分支不返回 `reply`，适配器会把上一轮问题重复播报
- **位置**：`interview_graph.py:296-302`（else 分支只返回 `current_phase/is_answering/raw_scores`）、`langgraph_adapter.py:110`
- **问题**：LangGraph 的 state 是增量合并，未返回的键沿用上一轮值；该轮 `reply` 仍是上一轮的综合问题，适配器 `result.get("reply")` 会原样再播一遍。
- **证据/影响**：同一复现中第 5 轮（回答综合题）`nodes=['compre_qa']、phase='report'、reply` 与第 4 轮问题字符串完全相同；下一次用户发言才真正执行 `node_report`。真实面试中考生会听到重复问题，报告也被推迟到一句无意义的补充发言之后。
- **修复建议**：该分支返回明确的过渡/结束台词；更彻底的做法是把“阶段推进”改成显式边（`compre_qa` 完成后条件边到 `report`），而不是依赖下一轮用户输入触发。

#### P1-4 Dify 兼容分支缺少 `import json`，SSE 解析必抛 `NameError`
- **位置**：`agent.py:243-244`（`json.loads` / `except json.JSONDecodeError`），文件 import 段 `agent.py:1-15` 无 `json`
- **问题**：`DifyLLMStream._handle_stream_line` 使用 `json` 但模块未导入；一旦收到任意 SSE 数据行即崩。
- **证据/影响**：以 HEAD 快照构造 `DifyLLMStream` 实例并调用 `_handle_stream_line(b'data: {"event":"message",...}')`，实测 `RAISED: NameError - name 'json' is not defined`。默认 `LLM_BACKEND=langgraph` 掩盖了该缺陷；一旦切到 Dify 旧模式，所有对话请求必失败。AST 静态检查与 pyflakes 类 lint 均可提前发现。
- **修复建议**：加 `import json`；为 SSE 解析补单测（message / message_end / error / 非 JSON 行）；若决定废弃 Dify，直接删除 `agent.py:109-296` 约 188 行遗留代码。

#### P1-5 适配器吞掉所有异常，用户只看到“系统处理出现异常”，无重试/无告警
- **位置**：`langgraph_adapter.py:125-132`
- **问题**：`except Exception` 记录日志后固定输出“系统处理出现异常，请稍后再试。”，不区分鉴权失败、超时、JSON 解析错误、编程错误（如 P1-4）；无重试、无指标上报、无错误码。
- **证据/影响**：任何 LLM/网络/代码错误都被降级成同一句话；配合“无超时”（见 P2-8）表现为长时间静默后一句道歉，故障不可观测。生产上属于“最危险的静默失败”模式。
- **修复建议**：区分可重试错误（`APITimeoutError`/连接）与不可重试错误（鉴权/参数），对可重试错误做有限重试并给用户“网络波动，请再说一次”；上报 metrics/结构化日志；保留原始异常链。

#### P1-6 后端与前端之间没有任何数据通道：阶段、转写、报告均不联动
- **位置**：后端全仓无 `publish_data`/`register_text_stream`（`git grep` 为空）；前端 `frontend/index.html:390-510` 无 `DataReceived` 处理、无 STT/转写订阅
- **问题**：前后端仅通过 LiveKit 音频耦合。前端“面试阶段指示器”与“实时文字记录”没有任何数据来源：`step1` 硬编码 `active`（`frontend/index.html:352`），`step2-4`（357/362/367）在 JS 中从未被引用；`transcriptBox` 只在连接/断开/说话状态时写入系统提示。
- **证据/影响**：README:26 声称“面试阶段指示器、实时文字记录”；实际阶段指示器永远停在第 1 步、转写区无任何面试内容。用户无法判断进度，也无法回看问答。
- **修复建议**：定义事件协议（`phase_changed` / `transcript` / `report`），后端在节点切换与 `user_input_transcribed`/`conversation_item_added` 时 `publish_data`；前端切换到 `topic` 订阅并用 `textContent` 渲染（勿用 innerHTML，见第 6 节）。

#### P1-7 `/api/token` 无鉴权 + CORS 通配 + 弱默认密钥
- **位置**：`server.py:13-14`（默认 `devkey`/`secret`）、`server.py:34`（`Access-Control-Allow-Origin: *`）、`server.py:47-64`（token 端点）；`livekit.yaml:7`（`devkey: secret`）
- **问题**：任何来源的 GET 请求都能为任意 `room`/`identity` 签发 6 小时有效的入房 token；默认密钥只有 6 字节（HS256 建议 ≥32 字节），代码在缺省环境变量时静默使用弱默认值而非启动失败。
- **证据/影响**：实测 `GET /api/token?room=ai-room&identity=attacker` 返回 200 与 token，响应头 `Access-Control-Allow-Origin: *`；JWT claims 为 `{"video":{"roomJoin":true,"room":"r1"},"sub":"i1","iss":"devkey","nbf":...,"exp":...}`，TTL 21600s；pytest 运行即出现 `InsecureKeyLengthWarning: The HMAC key is 6 bytes long`。任意网页可借用户浏览器进任意房间；若部署时未改默认 secret，攻击者可自行伪造 token 冒充任意身份。
- **修复建议**：token 端点加鉴权/Allowlist/限流，CORS 改为明确来源；启动时校验 LIVEKIT_API_SECRET 非默认值且长度足够，否则 fail-fast；缩短 TTL 并支持刷新；校验 room 命名与 identity 格式。

#### P1-8 Git 历史中残留明文 API Key
- **位置**：初始提交 `5233947` 的 `agent.py`；`6d2ffca` 才移除；`ac55bef:agent.py` 仍可检出 `DEFAULT_SILICONFLOW_API_KEY = "<redacted>"`（报告不复述完整值）
- **问题**：当前 HEAD `config.py` 已无硬编码密钥，但历史对象仍完整保留密钥明文；`git log --all -S 'sk-ngjb'` 可检索到。
- **证据/影响**：任何拿到仓库副本的人都能取回该密钥；若该 key 仍有效，等同凭据泄露。
- **修复建议**：**立即吊销/轮换该 SiliconFlow Key**（最高优先级）；仓库若需公开，使用 `git filter-repo` 清理历史；CI 加 gitleaks/trufflehog 类扫描。

### 2.3 P2（中等风险/可维护性）

#### P2-1 状态字段无 reducer，`raw_scores` 靠手工字符串拼接
- **位置**：`interview_graph.py:17-27`（`raw_scores: str` 裸字段），拼接点 `170/179/207/246/273/294/301`
- **问题**：LangGraph 对无 `Annotated` reducer 的字段是“后写覆盖”，当前靠每个节点手动 `state.get("raw_scores","") + ...` 累加；`node_report:307` 再整体读入提示词。任何新节点漏拼接、并行更新或重放都会丢面试记录，且拼接格式与提示词强耦合。
- **修复建议**：改为 `raw_scores: Annotated[list[str], operator.add]`（或 `Annotated[str, operator.add]`），由框架负责合并；报告节点统一渲染 transcript。

#### P2-2 `route_phase` 隐式路由与 `is_answering` 状态机耦合，完成后进入“死循环”
- **位置**：`interview_graph.py:25,185-193,252-259,360-370`
- **问题**：同一个节点既负责“出题”又负责“评卷”，靠 `is_answering` 布尔值区分；`current_phase` 只是普通 str；`route_phase` 对 `completed` 返回 `end`，图会直接返回旧 state。
- **证据/影响**：实测完成后再发言：`nodes=[]、phase='completed'`，state 不变，而适配器仍会把 `reply`（结束语）重播一遍——用户无法重新开始，也收不到任何回应。隐式布尔字段使状态机难以推理、难以测全。
- **修复建议**：拆成 `ask_question` / `grade_answer` / `advance_phase` 显式节点（或用 LangGraph `interrupt`/条件边表达等待），`current_phase` 用 `Literal`/Enum；`completed` 状态给出明确“面试已结束，可重新开始”出口并清理 thread。

#### P2-3 每个节点每次调用都新建 `ChatOpenAI` 客户端
- **位置**：`interview_graph.py:136-142`（`_get_llm`），调用点 `152/161/190/210/219/235/257/275/309`（`node_pro_qa` 有 4 个调用点，单次执行最多构造 3 个客户端）
- **问题**：每个节点按温度新建 ChatOpenAI 实例，重复构造配置对象，且 timeout/retry/日志等横切参数无处统一。实测两个 `_get_llm()` 返回对象不同（底层 httpx client 被 langchain 缓存复用，因此不是连接风暴，但仍是重复构造与配置漂移）。
- **修复建议**：模块级缓存按温度复用客户端，或用一个默认客户端 + `bind(temperature=...)`；在工厂里集中设置 `timeout`、`max_retries`、`metadata`。

#### P2-4 `pro_qa` 与 `compre_qa` 是大段复制粘贴
- **位置**：`interview_graph.py:183-247` 与 `250-302`；judge 判定式 `215` 与 `280` 完全相同
- **问题**：archive 拼接、judge 调用、`need_followup` 解析、追问分支、转阶段台词结构一一对应，仅提示词与字段名不同。任何一处修复（如 P1-3、P2-6）都要同步改两遍，已经在两处出现“阈值不一致”的苗头。
- **修复建议**：抽 `_handle_qa_turn(state, *, phase_cfg)` 公共节点函数，把提示词、state 字段名、转阶段动作参数化。

#### P2-5 报告解析失败时静默伪造分数（85/80/85）
- **位置**：`interview_graph.py:322-348`，默认值 `324-326`；提示词 `116-133`
- **问题**：LLM 返回缺字段或非 JSON 时，`data.get("total_score", 85)` 等默认值会让“解析异常”看起来像一份正常成绩单；`FINAL_REPORT_SYSTEM` 仅靠自然语言要求 JSON，未使用 `response_format`/JSON schema 校验，也未做 fence 外内容清洗失败的兜底策略说明。
- **证据/影响**：实测让 LLM 返回 `{}`，`node_report` 仍输出含“85 分”的成绩单（默认值），用户无法分辨“评了 85”与“没评出来”。
- **修复建议**：校验必需字段，缺失即重试一次（可带错误信息回灌）或显式标记“该项未生成”；使用 `with_structured_output`/JSON mode；不要用“看上去合理”的默认分。

#### P2-6 提示词阈值与代码阈值不一致，且代码覆盖 judge 结论
- **位置**：`interview_graph.py:36`（提示词“少于50字 → 不合格”）与 `interview_graph.py:159`（代码 `len(query) < 40`）
- **问题**：同一判定存在 40 字符/50 字两套标准；即使 judge 输出“合格”，代码仍按 `<40` 强制判不合格。
- **证据/影响**：模拟中一段 35 字、judge 明确返回“合格”的自我介绍，仍因 `len(query) < 40` 被判不合格（结果 `current_phase='self_intro'`），流程卡在 `self_intro`；用户看到的行为与提示词描述不一致。
- **修复建议**：阈值收敛到 `config` 常量并在提示词中引用同一数值；明确“代码阈值”与“模型判定”的唯一权威来源。

#### P2-7 开场白字符串匹配拦截：三处硬编码台词 + 脆弱耦合
- **位置**：`agent.py:176-185`（含 182 行固定台词）、`langgraph_adapter.py:88-99`（含 95 行固定台词）、`interview_graph.py:174`（另一句固定过渡语）
- **问题**：用“打招呼/自我介绍”等字面量判断是否为开场请求；三处台词不同步；若 `.env` 改了 `OPENING_PROMPT`（config.py:55-58）不再包含这些词，图会把“请向考生打招呼”当成考生自我介绍处理。
- **修复建议**：开场由首轮标志位（如 state 为空 / `turn_index==0`）触发，台词统一配置；拦截逻辑放在 adapter 之外的可复用入口。

#### P2-8 伪流式 + 无 usage/无超时
- **位置**：`langgraph_adapter.py:105-123`
- **问题**：先 `ainvoke` 等整段回复生成完，再按 12 字符/块 + `sleep(0.01)` 假装流式；sent chunk 不带 `usage`，无 token 计量；图调用无超时，LLM 卡住时本轮无限等待。
- **影响**：首块延迟 = 完整 LLM 生成时间（数秒），TTS 无法提前起播，与 README:19“无缝对接流式输出”不符；成本与性能不可观测。
- **修复建议**：节点内用 `astream` 把 token 直接转发到 `_event_ch`；结束 chunk 带 `usage`；对 `graph.ainvoke`/LLM 设置 `asyncio.wait_for` 超时并向上返回可重试错误。

#### P2-9 `MemorySaver` 全局共享、永不清理、thread 复用
- **位置**：`interview_graph.py:400-404`（模块级 compile + MemorySaver）、`langgraph_adapter.py:20-22`（thread_id = 参与者 identity）
- **问题**：所有会话共享同一个进程内 InMemorySaver；完成的 thread 从未删除；thread_id 由 participant identity 决定，同一身份重连会续用旧面试状态；worker 重启则全部丢失。
- **证据/影响**：实测单 thread 8 轮后保留 22 个 checkpoint（约每轮 2-3 个），随面试场次线性增长；长时间运行的 worker 存在内存泄漏与状态串场风险。
- **修复建议**：会话结束 `delete_thread`/定时 TTL 清理；thread_id 用“房间/会话 + 时间戳/随机数”；需要跨进程恢复时换持久化 checkpointer（SQLite/Redis）。

#### P2-10 预生成/重试可能重复推进面试状态（架构风险）
- **位置**：`langgraph_adapter.py:101-108`；对照 `AgentSession` 默认 `preemptive_generation=True`（本 venv livekit-agents 1.5.2 签名确认）
- **问题**：图的每次 `chat()` 调用都有外部副作用（推进 phase、发 LLM 请求、写 checkpoint），而 LiveKit 的预生成、打断重试、重生成都可能对“同一用户轮”调用多次；没有 turn id 幂等保护。
- **影响**：可能一次作答被评两轮、跳题或直接跳到 report；是否触发取决于 LiveKit 运行时行为，需在真实音频链路验证。
- **修复建议**：为每轮用户输入分配 turn id 并让图幂等；或对自定义 LLM 关闭 `preemptive_generation`；增加“同一轮重复调用不推进”的单测。

#### P2-11 配置双源 + `sk-dummy` 静默兜底 + 环境变量文档缺口
- **位置**：`config.py:22-60` 与 `server.py:13-16`（两套读取逻辑）；`agent.py:340/347`、`interview_graph.py:139`（`or "sk-dummy"`）；`.env.example:1-34` 未含 `AGENT_INSTRUCTIONS`/`OPENING_PROMPT`（config.py:51-58）
- **问题**：server 不读 `config.py`，`LIVEKIT_*` 默认值两处重复；缺少 Key 时不报错而是拿 `sk-dummy` 发请求（401 后表现为 P1-5 的通用道歉）；两个 Prompt 环境变量无法从模板发现。
- **修复建议**：统一 `Settings` 单例 + 启动校验（必需项缺失/仍是占位符即 fail-fast）；`.env.example` 补全全部变量并标注必填/默认值。

#### P2-12 依赖未锁定，环境不可复现
- **位置**：`requirements.txt:1-13`（`langgraph>=1.2.0`、`langchain-openai>=0.3.0`、`langchain-core>=0.3.0`、`pytest>=8.0.0`、`pytest-asyncio>=0.23.0`；`av` 未显式锁定）
- **问题**：无 lock 文件；原生依赖 `av`（PyAV，任务背景中提到的 DLL 故障来源）只由 livekit-agents 的 `av>=14.0.0` 间接决定；本 venv 甚至没有 pip（实测 `python -m pip --version` → `No module named pip`），README:41 的 `pip install -r requirements.txt` 在该 venv 内无法执行。
- **影响**：换机/重装可能得到不同 av/numpy/langchain 组合，PyAV 类环境故障难以复现与定位；CI 与面试机行为不一致。
- **修复建议**：用 uv/pip-tools 生成 lock；固定 `av`/`numpy`；文档写明 bootstrap（uv 或带 pip 的 venv）；CI 用锁文件安装。

#### P2-13 `server.py` 静态服务粗糙：单线程、无 Content-Length、MIME 覆盖不全
- **位置**：`server.py:76-86`（仅 html/css/js 设 Content-Type，无 Content-Length）、`server.py:90`（`HTTPServer` 单线程）
- **问题**：200 响应无 `Content-Length`（HTTP/1.0 关闭连接定界）；`.svg/.png/.json/.woff` 等无 MIME，浏览器靠嗅探；`HEAD` 请求返回 501；单线程下任一慢连接阻塞全部请求。
- **证据/影响**：原始 socket 实测 `/logo.svg` 返回 200 且无 `Content-Type`；`HEAD /` 返回 `HTTP/1.0 501 Unsupported method ('HEAD')`；所有 200 响应均无 `Content-Length`。
- **修复建议**：用 `ThreadingHTTPServer` + `mimetypes.guess_type`；显式写 `Content-Length`；错误响应也带 `Content-Type`；支持 HEAD；配置 `Cache-Control`。

#### P2-14 路径前缀校验可绕过（`startswith` 未带分隔符）
- **位置**：`server.py:67-70`
- **问题**：`safe_path.startswith(FRONTEND_DIR)` 只比较字符串前缀，未要求 `os.sep` 边界；`/../frontendX/secret.txt` 规范化后落在 `.../frontendX/...`，仍以 `.../frontend` 开头。
- **证据/影响**：实测 `GET /../frontendX/secret.txt` 返回 200 且内容为 `LEAKED-SECRET-FILE`（临时构造 `frontendX` 同级目录）。仓库内目前没有满足前缀的同级目录，故风险有限；但任何 `frontend-*`/`frontendX` 备份目录都会被读，且代码未显式拒绝含 `..` 的请求段。
- **修复建议**：`Path(FRONTEND_DIR).resolve()` + `candidate.resolve().is_relative_to(root.resolve())`；显式拒绝含 `..` 的请求段；路径检查加单测。

#### P2-15 测试覆盖窄、关键路径零覆盖（详见第 4 节）
- **位置**：`tests/test_graph_nodes.py`、`tests/test_graph_structure.py`、`tests/test_server.py`
- **问题**：8 个测试只覆盖 5 个节点 happy path、路由表、图可编译、token 可生成；`node_compre_qa` 被 import 但从未使用（`tests/test_graph_nodes.py:4`），`InterviewHttpHandler`/`HTTPServer` 被 import 但从未使用（`tests/test_server.py:3-4`）——恰是“计划测但没测”的证据。
- **修复建议**：按第 7 节测试补全清单，以“关键用户路径（开麦→问答→报告→前端可见）”为验收对象补集成测试。

#### P2-16 自我介绍阶段无重试上限、整场无总超时
- **位置**：`interview_graph.py:148-180`
- **问题**：judge 连续判“不合格”时图会一直停在 `self_intro`（每次用户发言重试一次），没有最大尝试次数，也没有整场时长上限。
- **影响**：考生可能永远无法进入下一阶段（面试无法结束、无成绩单）；与“限时模拟面试”的产品预期不符。
- **修复建议**：增加 `intro_attempts` 上限（例如 3 次后强制推进）与整场 deadline，超时直接生成报告。

### 2.4 P3（建议）

- **P3-1 未使用 import**：`server.py:2`（`logging`）；`tests/test_graph_nodes.py:4`（`node_compre_qa`）；`tests/test_server.py:1-4`（`pytest`/`os`/`InterviewHttpHandler`/`HTTPServer`）；`tests/test_graph_structure.py:1`（`pytest`）。建议引入 ruff/flake8 并清理。
- **P3-2 emoji 进入日志与报告文本**：`agent.py:177`（`⚡` 日志）在 cp936 控制台会触发 logging 的 `--- Logging error ---` 噪音；`interview_graph.py:330-343` 成绩单含 🎓/📊 等字符，一旦被 `print`/控制台输出同样会炸（本次模拟打印报告即复现 `UnicodeEncodeError`）。建议日志与终端输出只用 ASCII/中文，emoji 仅保留在 HTML。
- **P3-3 固定 `asyncio.sleep(1)` 后触发开场**：`agent.py:370`，依赖 1 秒猜测“参与者音频就绪”，慢网络下可能抢话。建议监听会话/轨道就绪事件或用 `generate_reply` 的等待语义。
- **P3-4 资源未显式释放**：`agent.py:356-377` 未注册 `ctx.add_shutdown_callback`；`LangGraphLLM.aclose`/`DifyLLM.aclose` 是空实现（`langgraph_adapter.py:54-55`、`agent.py:155-156`），LLM/HTTP 资源（共享 httpx client、MemorySaver）不随会话回收。
- **P3-5 `AGENT_INSTRUCTIONS` 在 LangGraph 模式下实际无效**：`agent.py:332` 把它传给 `voice.Agent`，但自定义适配器忽略 `chat_ctx` 中的 instructions，图内提示词完全由 `interview_graph.py` 决定；该配置项对当前默认后端是死配置，README 也未说明。
- **P3-6 `interview_word .yml`（git blob 74,233 B / 2,412 行；工作区 CRLF 约 76 KB）为无引用数据文件**：初始提交 `5233947` 引入，`git grep` 无任何代码读取；含 44 个工作流节点（Dify advanced-chat 导出）、文件名带空格；其内容（含 `final_report` 输出节点）与 `interview_graph.py` 语义重复，易被误当运行配置。建议移到 `docs/` 并注明“历史参考”，或删除。
- **P3-7 根目录 shell 脚本与实际运行环境错位**：`rebuild_venv.sh:6/18-19` 操作 `venv/`（Linux，而项目实际用 `.venv`）；`verify_agent_import.sh:5` 使用 `source venv/bin/activate`；`install_livekit_tools.sh:54-62` 直接改写 `~/.bashrc`/`~/.profile`、`64-68` 联网下载 GitHub Release（网络副作用）。实测本机 `livekit-server`/`lk`/`livekit-cli`/`docker` 均不在 PATH，README:84 的 `livekit-server --dev` 无法按文档执行。
- **P3-8 `livekit.yaml` 提交了 `devkey: secret`（:7）但 README 未使用它**（README 用 `--dev`），配置来源与实际启动方式脱节，容易让读者误以为已配置独立密钥。
- **P3-9 配置在 import 时被冻结、`load_dotenv()` 三处重复**：`config.py:6`、`agent.py:18`、`server.py:11`；测试无法通过环境变量改变 `config`，`PORT`（server.py:16）在 import 时即固定，非法值直接 `ValueError` 崩溃。
- **P3-10 `server.py` 细节**：404 响应无 `Content-Type`；OPTIONS 无 `Access-Control-Max-Age`；`log_message` 默认每请求向 stderr 打日志；无优雅退出；`0.0.0.0`（:90）把开发服务暴露到局域网（与 P1-7 叠加放大风险）。
- **P3-11 工程化缺失**：无 `pyproject.toml`/`pytest.ini`（`pytest-asyncio` 1.4.0 的 asyncio mode 未显式配置）、无 lint/类型检查配置、无 CI；`python -m pytest -q` 依赖“从仓库根目录运行 + 安装齐全”的隐式前提。
- **P3-12 可观测性缺失**：无结构化日志、无请求/会话 ID、无 token 用量与耗时指标（配合 P2-8/P1-5 使线上问题几乎不可定位）。

---

## 3. 架构专项评估

### 3.1 LangGraph 状态机设计
- **状态字段**：`InterviewState` 9 个字段（`interview_graph.py:17-27`）覆盖 phase、输入、回复、两个问题槽、评分原文与最终报告，字段本身够用；问题在于全部是裸 `str`/`bool`/`int`，phase 没有 `Literal` 约束，拼写错误不会在编译期暴露；`raw_scores` 采用“整段字符串累加”而非 reducer（P2-1）。
- **节点职责**：4 个节点承担了“判断 + 出题 + 评分 + 追问 + 转场”多种职责。`node_pro_qa`/`node_compre_qa` 用 `is_answering` 在“该出题”和“该评卷”之间切换（`185-205`、`252-270`），本质是“用一个布尔值编码状态机的当前状态”，可读性与可测性差（P2-2）。
- **路由**：`set_conditional_entry_point(route_phase)`（`384-393`）+ 每个节点直接 `add_edge(..., END)`（`395-398`），形成“每轮用户输入只走一个叶子节点”的 router 结构，而不是真正的阶段循环。结果：阶段推进只能发生在下一轮输入到来时（P1-3），且没有显式“等待用户”状态。
- **缺陷集中区**：`compre_qa` 的 report 分支不产生 reply（P1-3）、`report` 结果不外送（P1-1）、`self_intro` 无重试上限（P2-16）都源于“节点既决策又产出话术”这一设计。

### 3.2 Checkpoint、并发与线程隔离
- `interview_graph = create_interview_graph()` 在 import 时执行（`404`），`MemorySaver()` 为进程内全局单例（`400`）；所有 `LangGraphLLM` 共享同一 saver，仅靠 `thread_id` 隔离（`langgraph_adapter.py:20-22`）。
- 隔离粒度 = 参与者 identity（`agent.py:317-321`）。同一身份重连会续用旧面试；不同身份之间不会串场（这是当前设计中做对的部分）。但：完成的 thread 永不清理（实测单 thread 8 轮 22 个 checkpoint）、worker 重启状态全丢、多 worker 部署无法共享。
- `LangGraphLLM.api_lock`（`langgraph_adapter.py:23/101`）保证同一会话串行，避免并发写同一 thread；但它不解决“预生成/重试重复调用”的幂等性问题（P2-10）。
- 建议：短期 `thread_id = f"{room}:{identity}:{session_uuid}"` + 结束清理；中期换 SQLite checkpointer 并给会话设 TTL。

### 3.3 LangGraph ↔ LiveKit 适配层
- 实现方式正确：继承 `llm.LLM`/`llm.LLMStream`，`chat()` 抽取最新用户话语，`_run()` 驱动图并写 `ChatChunk`。本 venv 下用 `inspect.signature` 核对了 `AgentSession`、`generate_reply`、`OpenAI TTS/STT`、`VAD.load`、`create_api_error_from_http`、`LLMStream.__init__` 的实际签名，HEAD 调用方式与 livekit-agents 1.5.2 **兼容，未发现版本 API 错配**（这是值得肯定的部分）。
- 主要问题集中在语义层：伪流式（P2-8）、吞异常（P1-5）、忽略 `tools`/`instructions`/历史（图自持状态，可接受但需文档化）、开场白字符串匹配（P2-7）、重复的 `_extract_query`（`agent.py:47-58` 与 `langgraph_adapter.py:57-68` 两份实现）、无 usage 统计、无超时。
- “图即 LLM”的副作用模型与 LiveKit 的推测执行/重试语义存在张力（P2-10），建议在适配层显式声明“每次 chat 恰好消费一轮用户输入”。

### 3.4 Dify 兼容层
- `agent.py:109-296` 约 188 行（`DifyLLM` + `DifyLLMStream`）是历史兼容分支，`config.py:29-30` 用 `LLM_BACKEND` 切换。它当前**不可用**（P1-4 的 `json` NameError），且与 LangGraph 分支重复了开场白拦截、query 抽取、usage 解析等逻辑。
- SSE 解析只处理 `data:` 行、`message/agent_message/message_end/error` 四类事件，未处理 `[DONE]`、多行 data、`message_replace`；`conversation_id` 在共享 LLM 实例上可变（有 `api_lock` 兜底，但仅覆盖请求期间）。
- 建议：要么补齐 import + 单测后保留为降级通道，要么整体删除并同步 README/`.env.example`，避免“看似支持、实际必崩”的兼容层。

### 3.5 配置管理
- `config.py` 的 frozen dataclass 设计（`22-60`）方向正确、命名清晰；但默认值在类定义时求值（import 时定型），`load_dotenv()` 在 `config.py`、`agent.py`、`server.py` 各调一次，且 `server.py` 完全绕开 `config.py` 自读环境变量（`13-16`），两套默认值（如 `LIVEKIT_URL`）需要手工保持一致。
- 默认值偏“开发友好、生产危险”：`devkey/secret` 静默默认、`sk-dummy` 占位 API Key、`DIFY_BASE_URL=http://127.0.0.1/v1`（`.env.example:21`）；均缺乏启动校验。
- 缺失文档：`AGENT_INSTRUCTIONS`/`OPENING_PROMPT`（config.py:51-58）未出现在 `.env.example`。

### 3.6 模块边界与可测试性
- 模块拆分（agent / config / interview_graph / langgraph_adapter / server / frontend）清晰，`_get_llm` 可在测试中 patch，节点基本可单测，这是本项目最好的部分。
- 但可测试性的“缝”不足：`server.py` 把路由、token、静态文件、CORS 揉在一个 handler（无 app factory / 无 handler 单测）；`agent.py` 的 `entrypoint` 直接构造 session，无注入点；提示词以模块级字符串散落 10 个常量，无法按环境/i18n 组织；前端 512 行单文件内联 CSS+JS，无模块化与 lint。建议抽出 `session_factory`、`prompts.py`、`http_app` 三个缝后，关键路径才可做无音频集成测试。

---

## 4. 测试覆盖缺口

### 4.1 现有测试与覆盖映射

`pytest -q`（HEAD 快照副本，隔离目录）实测：**8 passed, 1 warning**。注意该 warning 即 P1-7 的弱密钥告警。

| 测试 | 实际验证 | 缺口 |
|---|---|---|
| `test_self_intro_unqualified_flow` | judge 不合格 + 引导话术 | 未覆盖“judge 合格但 len<40”的覆盖逻辑（P2-6） |
| `test_self_intro_qualified_flow` | 合格→pro_qa | 未验证 `raw_scores` 累计内容 |
| `test_pro_qa_initial_question` | 首题生成 | 未覆盖已完成出题/空问题边界 |
| `test_pro_qa_followup` | 追问 + 计数 | 未覆盖追问配额用尽后的转场 |
| `test_final_report_generation` | JSON 解析 happy path | 未覆盖缺字段默认分（P2-5）、坏 JSON 回退、最终报告送达 |
| `test_route_phase` | 直接调用路由函数 | 未通过图验证真实路由（`completed→end` 行为未被测出 P2-2） |
| `test_graph_compilation` | `graph is not None` | 弱断言，几乎零信息量 |
| `test_create_token` | token 是 JWT 字符串 | 未测 HTTP 处理器/CORS/静态文件/参数校验 |

### 4.2 完全缺失的行为测试
1. `node_compre_qa` 全阶段（出题/判分/追问/转 report）——import 了但从未使用（`tests/test_graph_nodes.py:4`）。
2. `LangGraphLLM`/`LangGraphLLMStream`：开场拦截、异常吞噬、重复回复、块切分、usage 缺失、无超时。
3. `agent.py`：`_extract_query`/`_safe_identity`/`_message_to_text`/`_extract_usage`、`_build_session` 的 backend 选择、Dify SSE 解析（该测试可直接抓出 P1-4）。
4. HTTP 处理器：静态文件 200/404、MIME、CORS/OPTIONS、路径穿越（P2-14）、token 参数校验与 TTL。
5. 多轮集成：带 checkpointer 的完整图（阶段序列/题目数量/完成后行为）、thread 隔离与清理。
6. **没有任何测试断言“成绩单/阶段/转写能被客户端消费”**——所以 P1-1/P1-6 这类核心功能缺失能在 8 个绿灯测试下长期存在。
7. 配置与启动：`_build_session` 缺 Key 时的行为、`server.py` 启动 smoke（所以 P0-1 未被发现）。

### 4.3 “假测试/弱断言”风险
- `test_graph_compilation` 只断言非 None；`test_route_phase` 绕过图直接调用路由函数，无法发现入口边/END 边的接错。
- 节点测试全部 `patch("interview_graph._get_llm")` 返回同一个 mock，隐藏了“每节点重复构造客户端”“温度参数”“调用次数”（例如转场轮应 2-3 次 LLM 调用）等行为。
- README:74 称“完整的单元测试与状态机模拟测试”，实际是 8 个以 mock 为主的小测试，无状态机级模拟（多轮状态演进）与端到端测试，属过度表述。

### 4.4 环境性失败与复现记录（如实记录）
- **`livekit.agents` 导入失败（任务背景提到的 PyAV DLL 故障）在本机未复现**：`python -c "import livekit.agents"` → OK 1.5.2；`import av` → OK 19.0.2；`import livekit.rtc`/`from livekit import rtc` → OK；`import agent`、`import langgraph_adapter` 均 OK。因此本报告不把它计为当前代码缺陷，但记录为**环境风险**：`requirements.txt` 未锁 `av`（仅由 livekit-agents 的 `av>=14.0.0` 决定），重装/换机后出现 PyAV DLL 类故障的概率存在，且本 venv 无 pip、无 lock，问题难以复现固定（见 P2-12）。
- **cp936 启动崩溃可稳定复现**（P0-1），属“环境 × 代码”耦合的确定性故障。
- `pytest -q` 在 HEAD 快照上 8 passed；当前工作区正被并行修改，本报告不以其为准。
- 本机 `livekit-server`/`lk`/`livekit-cli`/`docker` 均不在 PATH，无法按 README 完成真实房间联调（`agent.py`/前端与 LiveKit 的联动未做端到端验证，相关结论仅基于代码与 LiveKit API 签名核对）。

---

## 5. 安全与配置风险

1. **token 签发无鉴权（P1-7）**：`/api/token` 对任意来源开放，可指定任意 `room`/`identity`；JWT 有效期 6 小时（实测 `exp-nbf=21600`），无刷新、无身份校验。修复：加鉴权/限流 + 房间白名单 + 短 TTL。
2. **CORS 通配（P1-7）**：`server.py:34` 对所有响应（含 token）发送 `Access-Control-Allow-Origin: *`，任意网页可跨域读取 token；应改为显式来源列表，token 端点禁止通配。
3. **默认弱密钥/开发密钥入库（P1-7/P3-8）**：`server.py:13-14`、`livekit.yaml:7`、`.env.example:7-8` 均为 `devkey/secret`；pytest 直接打印 HMAC 密钥长度告警（6 bytes）。应启动时校验并拒绝默认值。
4. **路径前缀绕过（P2-14）**：`startswith` 未加分隔符，实测可读取 `frontend*` 同级目录文件；应 `resolve()` + `is_relative_to`。
5. **Git 历史泄密（P1-8）**：初始提交中的明文 SiliconFlow Key 仍在历史中，必须吊销轮换 + 历史清理 + secret 扫描。
6. **依赖供应链（P2-12 / 第 6 节 F-7）**：requirements 使用宽带版本约束、无 lock；前端从 CDN 加载 livekit-client 且无 SRI/本地回退。建议锁依赖、CDN 加 `integrity`+`crossorigin` 或本地托管。
7. **暴露面（P3-10）**：`HTTPServer(("0.0.0.0", port))` 绑定全接口，token 端点与静态服务暴露到局域网；开发工具应默认绑 `127.0.0.1`。

---

## 6. 前端原状问题（`git show HEAD:frontend/index.html`，512 行）

- **F-1（P1）阶段步进器完全静态**：`step1` 在 HTML 中硬编码 `class="flow-step active"`（:352），`step2/3/4`（:357/362/367）在整份 JS 中再无引用（grep 确认）；无数据通道/阶段事件驱动。用户全程看到“自我介绍”高亮。
- **F-2（P1）无实时转写**：转写区 `transcriptBox` 只有 `log()` 写入（:401-411），调用点仅为连接成功/断开/音频接通/麦克风开启（:437、:477、:482、:486）；未订阅 STT 结果，也未渲染任何考生/考官话语。后端同样未发布转写（P1-6）。
- **F-3（P1）无成绩单展示**：页面没有分数/维度/建议的 DOM 容器，`step4`“综合成绩单”永远不激活；报告只存在于后端 state（P1-1）。README:26 的“成绩单”为未实现承诺。
- **F-4（P2）`innerHTML` XSS 注入面**：`log()` 用 ``line.innerHTML = `<span ...>${role}</span> ${text}` ``（:408）拼接动态内容；当前 `text` 主要是系统文案，`role` 来自固定调用点，但 `roomName` 来自 token 接口响应（:482），一旦未来把转写/报告/身份写进 `log()`，即为可利用的注入点。修复：`textContent` + `createElement`。
- **F-5（P2）音频 track 泄漏与断线状态残留**：`TrackSubscribed` 中 `track.attach()` 后 `document.body.appendChild(audioElement)`（:433-439），但从未监听 `TrackUnsubscribed`、也从未 `detach()`/`remove()`；`Disconnected`（:469-478）只改 UI 不动音频元素，且未把 `currentRoom` 置空（仅 `stopInterview` 置空 :501-506），重连/多次进入会累积 `<audio>` 与处理器；无 `Reconnecting/Reconnected` 处理。
- **F-6（P2）移动端溢出/裁切**：唯一响应式规则只改 `.main-stage` 为单列（:130-134）；`.flow-indicator`（:200-238）是“4 步 + 3 箭头”的单行 flex，`.container` 设置 `overflow: hidden`（:48）。在 390×844 视口实测（`.review/before-mobile.png`）第 4 步“综合成绩单”与顶部状态徽标被右侧裁切。修复：`flex-wrap`/两行网格/横向滚动 + 小屏字号。
- **F-7（P2）外部依赖无完整性校验、离线不可用**：`<script src="https://cdn.jsdelivr.net/npm/livekit-client@2.11.0/...">`（:8）无 `integrity`/`crossorigin`，断网时页面直接白屏（无加载失败提示/本地 fallback）。
- **F-8（P3）错误处理与重连体验**：连接失败用阻塞式 `alert()`（:494），无重连/设备切换/麦克风权限拒绝的针对性提示；`statusText` 只有粗粒度文案。
- **F-9（P3）可访问性与健壮性**：emoji 头像无 `aria-hidden`、转写更新无 `aria-live`；`transcript-box` 固定 `max-height: 180px`；按钮无 `aria-busy`；`currentRoom` 未在页面卸载时清理（:501-506）。

---

## 7. 修复优先级清单（建议执行顺序）

1. **P0 修复启动崩溃**（0.5h）：`server.py:91` 去 emoji/改 logging/`sys.stdout.reconfigure`；加“启动 smoke test”；验收：`chcp 936` 下 `python server.py` 正常监听且 `GET /` 返回 200。
2. **P1 修复 Dify 分支**（0.2h 或删除）：`agent.py` 加 `import json`；加 SSE 单测；若不维护则删除 `agent.py:109-296` 并同步 README/`.env.example`。
3. **P1 打通“报告 + 阶段 + 转写”数据通道**（1-2d）：后端在 `node_report`/阶段切换/转写事件时 `publish_data`；前端加 `DataReceived` 渲染成绩单、激活步进器、用 `textContent` 写转写；验收：完成一场面试后页面出现分项成绩与问答文本。
4. **P1 修正面试深度与重复提问**（0.5-1d）：`compre_qa → report` 分支返回过渡台词（或改条件边直接进 report）；state 增加题目计数，每阶段出 3-5 题；验收：假 LLM 集成测试断言题目数与阶段序列。
5. **P1 安全修复**（0.5d）：轮换历史泄漏的 SiliconFlow Key；token 端点加来源校验/限流、CORS 收紧、默认 secret fail-fast、TTL 缩短；验收：无 Origin/非白名单请求被拒。
6. **P2 适配器与可靠性**（1d）：真实流式（`astream`）、超时、usage、结构化错误与有限重试；评估关闭 `preemptive_generation` 或引入 turn 幂等。
7. **P2 状态机重构**（1d）：`raw_scores` 用 reducer、`current_phase` 用 Literal、抽公共 QA 节点、统一阈值常量、清理完成后 thread。
8. **P2 前端质量**（1d）：音频 track 清理、断线状态复位、移动端布局、CDN SRI/本地化、转写渲染安全化。
9. **P2 服务端加固**（0.5d）：`ThreadingHTTPServer`、`resolve+is_relative_to` 路径校验、MIME 表、Content-Length、HEAD、错误响应头。
10. **P2 测试与工程化**（1-2d）：补第 4 节缺口测试（尤其适配器/HTTP/多轮集成/报告可见性）、加 `pyproject.toml`（asyncio mode、coverage、ruff/mypy）与 CI、锁依赖。
11. **P3 清理**（0.5d）：未使用 import、死配置、emoji 日志、`interview_word .yml` 归档到 `docs/`、shell 脚本对齐 Windows/`.venv`、README 与实现对齐。

---

## 附录 A：关键复现命令与实测摘要

```
# 1) HEAD 基线测试（隔离快照目录）
python -m pytest -q            -> 8 passed, 1 warning
warning: InsecureKeyLengthWarning: The HMAC key is 6 bytes long ...

# 2) cp936 启动崩溃（FRONTEND_PORT=8093）
chcp                            -> Active code page: 936
python server.py                -> exit 1
  File "server.py", line 91, in run_server
    print(f"\U0001f680 Custom Interview Frontend running at ...")
  UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f680'

# 3) Dify SSE 解析（HEAD 快照）
DifyLLMStream._handle_stream_line(b'data: {"event":"message",...}')
  -> NameError: name 'json' is not defined

# 4) 图多轮行为（假 LLM patch interview_graph._get_llm）
turn1 self_intro -> pro_qa
turn2 pro_qa     -> 出第 1 道专业题
turn3 pro_qa     -> judge 不需要 -> compre_qa
turn4 compre_qa  -> 出第 1 道综合题
turn5 compre_qa  -> phase=report，但 reply 仍为上一轮问题（重复播报）
turn6 report     -> completed，final_report 仅存于 state
完成后继续发言    -> nodes=[]，reply 不变（结束语被重复播放）
      单 thread 8 轮 -> 22 个 checkpoint，未清理

# 5) 假 LLM 返回 "{}" 时 node_report
-> 成绩单仍出现“85 分”（默认值伪造）

# 6) server.py 原始 HTTP 探测（临时目录 + 进程内启动）
GET /                     -> 200, 无 Content-Length
GET /logo.svg             -> 200, 无 Content-Type（MIME 缺失）
GET /../frontendX/secret.txt -> 200, body=LEAKED-SECRET-FILE（前缀绕过）
HEAD /                    -> HTTP/1.0 501 Unsupported method
GET /api/token?...        -> 200 + token, ACAO: *
JWT claims                -> {"video":{"roomJoin":true,"room":"r1"},"sub":"i1","iss":"devkey","nbf":...,"exp":...}
                             exp-nbf = 21600s

# 7) 环境核对
python -c "import livekit.agents; import av"      -> OK（本次未复现 PyAV 故障）
python -c "import agent; import langgraph_adapter" -> OK
python -m pip --version                            -> No module named pip
Get-Command livekit-server/lk/livekit-cli/docker   -> NOT FOUND
```

## 附录 B：审查文件清单（HEAD 行数）

| 文件 | 行数 | 备注 |
|---|---|---|
| `agent.py` | 385 | 含 188 行 Dify 遗留分支 |
| `config.py` | 62 | frozen dataclass，import 时定型 |
| `interview_graph.py` | 404 | 4 节点 + 路由 + MemorySaver |
| `langgraph_adapter.py` | 132 | LiveKit LLM/LLMStream 适配 |
| `server.py` | 96 | token + 静态文件 |
| `frontend/index.html` | 512 | 内联 CSS+JS 单文件 |
| `tests/test_graph_nodes.py` | 92 | 5 个节点测试 |
| `tests/test_graph_structure.py` | 15 | 2 个结构测试（其中 1 个弱断言） |
| `tests/test_server.py` | 11 | 仅 token 测试 |
| `requirements.txt` | 13 | 部分 `>=`，无 lock |
| `.env.example` | 34 | 缺 2 个 Prompt 变量 |
| `README.md` | 118 | 多处与实现不符 |
| `livekit.yaml` | 10 | 明文 devkey/secret |
| `interview_word .yml` | 2,412 | 74,233 B，Dify 导出，无代码引用 |
| 根目录 `*.sh`（5 个） | 合计约 140 | WSL/Linux 假设，与 `.venv`/Windows 文档不一致 |

> 说明：本报告仅修改 `docs/code-review.md`，未改动任何源码、测试或配置；所有结论均可按附录 A 复现。

---

## 附录 C：本轮修复进展（报告完成后的变更）

> 本报告的结论与行号均基于 `HEAD 63c4a0c` 原状；此后工作区已完成一轮修复与前端重写，状态如下（明细与验证证据见 [optimization-report.md](optimization-report.md)）。未列出的条目仍按原建议待评估。

| 报告条目 | 状态 | 处理说明 |
| --- | --- | --- |
| P0-1 cp936 启动崩溃 | ✅ 已修复 | `server.py` 重写：logging + UTF-8 安全流，cp936 冒烟通过 |
| P1-1 `final_report` 不可达 | ✅ 已修复 | 新增数据通道 `report` 事件，前端成绩单面板消费 |
| P1-2 每阶段仅 1 题 | ✅ 已修复 | 新增 quick / real 双模式（1 题 vs 3 题/阶段，追问 1 vs 2） |
| P1-3 `compre_qa→report` 无 reply、重复播报 | ✅ 已修复 | 最后一轮答完同轮生成报告并返回结语 |
| P1-4 Dify 分支缺 `import json` | ✅ 已修复 | 先补 import + SSE 单测；后按用户确认整体移除 Dify 兼容层（含 `interview_word .yml`） |
| P1-5 适配器吞异常 | ⚠️ 部分 | 仍为统一兜底话术；新增 `listening` 状态事件，结构化错误/重试待后续 |
| P1-6 前后端无数据通道 | ✅ 已修复 | topic=`interview` 的 status/phase/report/mode 协议，前端订阅 |
| P1-7 token 无鉴权 + 弱默认密钥 | ⚠️ 部分 | CORS 通配与路径校验已修；鉴权/限流/密钥校验属部署侧，见 README 安全提示 |
| P1-8 历史明文 API Key | ❌ 待用户处理 | 需吊销轮换（代码侧无法安全自动清理历史） |
| P2-5 报告解析失败伪造分数 | ✅ 已修复 | 缺失/非法 `total_score` 视为失败，不再编造默认分 |
| P2-6 prompt/code 阈值不一致 | ✅ 已修复 | 统一 `MIN_INTRO_CHARS`（默认 50） |
| P2-8 伪流式、无超时 | ⚠️ 部分 | 已改 LangGraph `custom` stream 真流式；LLM 超时/usage 待后续 |
| P2-10 预生成重复推进 | ✅ 缓解 | `AgentSession(preemptive_generation=False)` |
| P2-13 静态服务粗糙 | ✅ 已修复 | ThreadingHTTPServer、MIME、Content-Length、缓存、404/405/500 |
| P2-14 路径前缀可绕过 | ✅ 已修复 | unquote + realpath + commonpath，含穿越用例测试 |
| P2-16 自我介绍无重试上限 | ✅ 已修复 | `intro_attempts` + 模式化上限，超限强制推进 |
| P3-1 未使用 import / emoji 日志 | ✅ 已修复 | 清理相关 import 与日志 emoji |
| P3-5 `AGENT_INSTRUCTIONS` 死配置 | ⚠️ 保留 | 仍由 `voice.Agent` 接收，LangGraph 后端下不生效（文档已注明） |
| P3-6 `interview_word .yml` | ✅ 已处理 | 经用户确认，该文件是 Dify 工作流导出、可删除；已随 Dify 兼容层一并移除 |
| P2-1/2/3/4/9、P2-7/11/12/15、P3-2~4、P3-7~12 | ❌ 未处理 | 结构性重构/工程化项，见报告第 7 节优先级 |

# 第 4 章：面试状态机（interview_graph.py）

这是整个项目最“有业务”的文件。它不关心音频、不关心 WebRTC，只回答一个问题：

> 给定到目前为止的面试状态和考生刚说的话，下一句该说什么？

---

## 4.1 为什么是状态图

如果用一堆 `if/else` 写面试流程，会很快变成这样：

```python
if phase == "self_intro":
    if not qualified and attempts < max:
        ...
    elif not qualified:
        phase = "pro_qa"
    ...
elif phase == "pro_qa":
    if is_answering:
        if need_followup and follow_up_count < max:
            ...
```

这些逻辑本身没错，但：

- 状态散落在局部变量里，无法回放；
- 每轮都要手动拼接“前面的问答记录”；
- 测试只能测单个函数，难测完整多轮；
- 想加一个阶段，要动很多分支。

LangGraph 把状态变成**显式的数据**，把每个决策变成**节点函数**，把“下一轮去哪个节点”变成**纯函数路由**。checkpointer 按 `thread_id` 保存状态，于是多轮对话自然成立。

---

## 4.2 状态定义

```python
class InterviewState(TypedDict):
    current_phase: str            # self_intro | pro_qa | compre_qa | report | completed
    user_query: str               # 本轮考生说的话
    reply: str                    # 本轮考官要说的台词
    follow_up_count: int          # 当前题目已追问次数
    question_pro: str             # 当前专业题
    question_compre: str          # 当前综合题
    is_answering: bool            # 当前是否有待回答的问题
    intro_attempts: int           # 自我介绍已重试次数
    interview_mode: str           # quick | real
    questions_per_phase: int      # 每阶段主问题数（可被 state 覆盖）
    questions_asked: int          # 当前阶段已出题数
    max_follow_ups: int           # 最大追问数（可被 state 覆盖）
    max_intro_attempts: int       # 自我介绍最大重试（可被 state 覆盖）
    asked_questions: str          # 已问过的问题，防重复
    raw_scores: str               # 全程文字记录
    final_report: str             # 纯文本成绩单
    report_data: dict[str, Any]   # 结构化成绩单，前端用
```

### 最重要的语义：返回的 dict 是增量

LangGraph 对普通字段使用“后写覆盖”的 channel。节点返回：

```python
return {"reply": "...", "current_phase": "pro_qa"}
```

没返回的字段（例如 `raw_scores`、`question_pro`）**保留上一轮的值**。这带来两个后果：

1. 好事：不用每轮把整个 state 都返回。
2. 坑：如果某轮忘了更新 `reply`，适配器会把上一轮的 `reply` 再播一遍。历史 bug 就是这么来的。

所以每个节点都要问自己：**这轮结束后，哪些字段必须显式改？**

### 为什么 `raw_scores` 是字符串而不是 list

LangGraph 支持 `Annotated[list[str], operator.add]` 这种 reducer，让框架自动合并。但这里仍然是字符串手工拼接：

```python
archive = state.get("raw_scores", "") + f"【专业提问】\n{current_q}\n【考生回答】\n{query}\n"
```

原因是报告提示词需要一段连续的中文文本，而不是 JSON 数组；字符串拼接最直接。代价是每个节点都要记得带上 `raw_scores`，漏一处就丢记录。测试里会断言阶段记录数量，防止回归。

> 如果想重构，可以把 `raw_scores` 改成 `Annotated[list[dict], operator.add]`，再在报告节点统一渲染。这是后续可选项，不是当前必需。

---

## 4.3 提示词设计

文件开头是一组 `*_SYSTEM` 常量。它们不是随便写的，每个都对应一个“窄任务”：

| 常量 | 任务 | 输出要求 |
| --- | --- | --- |
| `INTRO_JUDGE_SYSTEM` | 判断自我介绍是否合格 | 只能“合格/不合格” |
| `UNQUALIFY_SYSTEM` | 让考生补充自我介绍 | 100 字左右，纯台词 |
| `PRO_QUESTION_SYSTEM` | 出一道专业课问题 | 50 字内，一道题 |
| `PRO_JUDGE_SYSTEM` | 判断是否需要追问 | 只能“需要/不需要” |
| `PRO_FOLLOWUP_SYSTEM` | 生成一次追问 | 50 字内，单点 |
| `TRANSITION_TO_COMPRE_SYSTEM` | 专业→综合的过渡 | 40 字内 |
| `COMPRE_QUESTION_SYSTEM` | 出综合题 | 60 字内，一道题 |
| `COMPRE_JUDGE_SYSTEM` | 判断综合题是否追问 | 只能“需要/不需要” |
| `COMPRE_FOLLOWUP_SYSTEM` | 综合追问 | 50 字内 |
| `FINAL_REPORT_SYSTEM` | 生成 JSON 成绩单 | 必须合法 JSON |

### 窄任务原则

不要让一个提示词同时做“出题 + 判分 + 决定下一步”。模型在长指令下更容易漂移。这里把“判断”和“生成”分开：

```text
判分（temperature=0.1）→ 需要/不需要
   │
   ├─ 需要 → 生成追问（temperature=0.4）
   └─ 不需要 → 出下一题（temperature=0.6）
```

温度也不同：

- 判分要稳定 → 0.1；
- 追问要有锋芒 → 0.4；
- 出题要有变化 → 0.6 / 0.7；
- 成绩单要克制 → 0.2。

### 判分输出的容错

提示词要求“只能输出需要/不需要”，但模型仍可能输出“不需要，回答很完整”。所以代码不直接比较相等：

```python
def _needs_followup(judge_text: str) -> bool:
    normalized = judge_text.strip()
    return "需要" in normalized and "不需要" not in normalized
```

先匹配否定词，再匹配肯定词。这样“不需要”不会被当成“需要”。

### 自我介绍阈值

提示词写“少于 50 字 → 不合格”，代码也读同一个配置：

```python
too_short = len(query) < config.min_self_intro_chars
unqualified = "不合格" in result_text or too_short
```

两边共用 `MIN_INTRO_CHARS`，避免“提示词说 50、代码判 40”的分裂。注意：代码阈值是**或**关系，即使模型说合格，太短仍然不合格。这是有意的——模型可能被一段空话骗过。

---

## 4.4 LLM 工厂与流式回复

### `_get_llm`

```python
@lru_cache(maxsize=8)
def _get_llm(temperature: float = 0.5) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.llm_model,
        api_key=config.llm_api_key or "sk-dummy",
        base_url=config.llm_base_url,
        temperature=temperature,
        timeout=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )
```

为什么缓存：

- 同一温度反复构造客户端没有意义；
- `timeout` / `max_retries` 在一个地方配置，不会某个调用点漏掉；
- `lru_cache` 的 key 是温度，最多 8 个，足够。

为什么 `or "sk-dummy"`：没有 Key 时模块仍能导入、测试仍能跑；真实请求会 401，不会静默成功。

### `_reply_text`：把 token 流向 UI

```python
async def _reply_text(llm, messages) -> str:
    if not isinstance(llm, BaseChatModel):
        result = await llm.ainvoke(messages)
        return str(result.content).strip()

    writer = get_stream_writer()
    parts: list[str] = []
    async for chunk in llm.astream(messages):
        text = chunk.content
        if isinstance(text, str) and text:
            parts.append(text)
            writer({"type": "delta", "text": text})
    return "".join(parts).strip()
```

两个分支：

- **真实模型**是 `BaseChatModel`，走 `astream`，每拿到一个 token 就 `writer(...)`。LangGraph 会把它送进 `custom` 流，适配器再送给 TTS。首字延迟从“整段生成完”降到“第一个 token”。
- **测试替身**（`MagicMock`、`FakeListChatModel` 之外的简单 mock）不是 `BaseChatModel`，走 `ainvoke`，一次性返回。这样单元测试不需要模拟 token 流。

> `get_stream_writer()` 只有在 LangGraph 运行上下文里才有效。所以 `_reply_text` 必须在节点内部调用，不能拿到图外单独跑。

---

## 4.5 模式与预算

```python
INTERVIEW_MODES = {
    "quick": {
        "label": "简单模式",
        "questions_per_phase": config.quick_questions_per_phase,
        "max_follow_ups": config.quick_max_follow_ups,
        "max_intro_attempts": config.quick_max_intro_attempts,
    },
    "real": {
        "label": "实战模式",
        "questions_per_phase": config.real_questions_per_phase,
        "max_follow_ups": config.real_max_follow_ups,
        "max_intro_attempts": config.real_max_intro_attempts,
    },
}
DEFAULT_INTERVIEW_MODE = DEFAULT_MODE
```

`_interview_settings(state)` 的解析顺序：

```text
state 里的显式覆盖（questions_per_phase 等）
        │ 没有
        ▼
INTERVIEW_MODES[mode] 的默认值
        │
        ▼
非法值 / 负数 → 回退默认；负数夹到 0
```

为什么 state 可以覆盖：测试或未来 API 可以按场次定制题量，而不必改环境变量。

为什么负数要夹到 0：`max(0, value)` 防止配置错误导致无限追问或负数循环。

---

## 4.6 节点逐个拆

### `node_self_intro`

```text
输入：user_query（考生自我介绍）
1. 判分 LLM（temperature=0.1）→ 合格 / 不合格
2. 代码再按长度判一次
3. 不合格 且 attempts < max_intro_attempts
      → 生成引导语，current_phase 保持 self_intro，attempts+1
4. 不合格 且 次数用尽
      → 固定过渡语，current_phase=pro_qa，questions_asked=0
5. 合格
      → 固定过渡语，current_phase=pro_qa，questions_asked=0
```

关键返回：

```python
return {
    "reply": reply_text,
    "current_phase": "self_intro",
    "is_answering": True,
    "intro_attempts": attempts + 1,
    "raw_scores": state.get("raw_scores", "") + f"【自我介绍】\n{query}\n",
}
```

为什么 `is_answering=True`：表示“我刚刚问了一个需要考生回答的问题”。这个字段主要给 `pro_qa/compre_qa` 用，self_intro 不读它，但语义保持一致。

为什么重试上限用尽也要继续：面试必须能结束。宁可放过一个不合格的自我介绍，也不能让考生永远卡在第一阶段。

### `node_pro_qa`

这是最复杂的节点，因为它同时负责“出题”和“评上一题”。用 `is_answering` 分流：

```text
if not is_answering or not current_q:
    → 出题分支
else:
    → 评卷分支
```

#### 出题分支

```python
prompt = (
    "请向考生提出第一道专业课面试题。"
    if questions_asked == 0
    else "请向考生提出下一道专业课面试题，不要重复之前问过的问题。"
)
question = await _reply_text(llm, [
    SystemMessage(content=PRO_QUESTION_SYSTEM),
    HumanMessage(content=_question_prompt(prompt, state)),
])
return {
    "question_pro": question,
    "reply": question,
    "is_answering": True,
    "follow_up_count": 0,
    "questions_asked": questions_asked + 1,
    "asked_questions": _remember_question(state, question),
}
```

注意：

- `follow_up_count` 归零：新题目的追问配额重新开始。
- `questions_asked + 1`：主问题计数。
- `asked_questions`：把题目追加到“已问过”列表，下一次出题提示词会带上它。

#### 评卷分支

```text
archive = raw_scores + 本轮问答
judge = PRO_JUDGE_SYSTEM + (当前问题, 考生回答)
need_followup = _needs_followup(judge)

if need_followup and follow_up_count < max_follow_ups:
    → 生成追问；follow_up_count+1；is_answering=True；记录 archive
elif questions_asked < questions_per_phase:
    → 出下一题；follow_up_count=0；questions_asked+1；记录 archive
else:
    → 生成过渡语；current_phase=compre_qa；questions_asked=0；reset asked_questions
```

为什么先判追问再判下一题：追问是对当前题目的深挖，必须在换题之前。

为什么追问也要记入 `asked_questions`：下一道主问题不应该重复追问里已经问过的角度。

### `node_compre_qa`

结构与 `node_pro_qa` 几乎对称，只是提示词、state 字段（`question_compre`）和温度不同。最后的“题量用尽”分支不是过渡到下一阶段，而是**直接生成报告**：

```python
report_result = await _generate_report({**state, "raw_scores": archive})
report_result["raw_scores"] = archive
return report_result
```

为什么在同一回合生成：如果返回 `current_phase="report"` 然后等下一轮，前端会等一句考生根本没理由说的话。历史 bug 就是“报告被推迟一轮 + 重复播报上一题”。

`{**state, "raw_scores": archive}` 是浅拷贝，只替换记录；其余字段（mode、attempts 等）原样传给报告节点。

### `node_report` 与 `_generate_report`

`node_report` 只是一个薄包装：

```python
async def node_report(state):
    return await _generate_report(state)
```

它保留给图里的 `report` 路由（虽然当前没有节点会设置 `current_phase="report"`）。这样未来想加“单独重生成报告”的入口，不用改图。

`_generate_report` 做四件事：

1. 调 LLM，要求 JSON；
2. 去掉可能的 ```` ```json ```` 围栏；
3. `json.loads`；
4. `_build_report_payload` 规范化。

解析失败时：

```python
return {
    "reply": "面试已结束，非常感谢同学的配合，稍后可在后台查看详细报告。",
    "final_report": f"面试结束。\n{raw_text}",
    "report_data": {},
    "current_phase": "completed",
    "is_answering": False,
}
```

**不伪造分数**。`report_data={}` 让前端显示“报告生成失败/原始文本”，而不是编一个 85 分。这比“看起来正常”更有诚信。

### `_build_report_payload`

```python
try:
    total_score = int(round(float(data.get("total_score"))))
except (TypeError, ValueError):
    raise ValueError("report JSON is missing a usable total_score")
```

- `total_score` 是必需字段；缺失即失败。
- 分项分数可以缺失，用默认值，但会夹在 `[0, 100]`。
- `core_weakness` / `improvement_tips` / `closing` 缺失时用中文默认值。

为什么不用 pydantic/JSON schema：当前只有一个结构，手写校验更轻；如果以后报告维度变多，可以换成 `with_structured_output`。

### `node_completed`

面试结束后用户又说话：

```python
return {
    "reply": "面试已经结束了。如需重新开始，请退出考场后重新进入。",
    "current_phase": "completed",
    "is_answering": False,
}
```

没有这个节点，`route_phase` 返回 `"end"`，图直接返回旧 state，适配器会把上一轮的结语再播一遍。

注意：这里没有清空 `report_data`。state 里仍然保留成绩单，适配器通过 `_report_emitted` 保证只发一次。这样既不会重复弹窗，又保留了状态（未来可查询）。

---

## 4.7 路由与图构建

```python
def route_phase(state) -> Literal[...]:
    phase = state.get("current_phase", "self_intro")
    if phase == "self_intro": return "self_intro"
    if phase == "pro_qa":     return "pro_qa"
    if phase == "compre_qa":  return "compre_qa"
    if phase == "report":     return "report"
    return "end"
```

```python
def create_interview_graph():
    builder = StateGraph(InterviewState)
    builder.add_node("self_intro", node_self_intro)
    builder.add_node("pro_qa", node_pro_qa)
    builder.add_node("compre_qa", node_compre_qa)
    builder.add_node("report", node_report)
    builder.add_node("completed", node_completed)

    builder.set_conditional_entry_point(
        route_phase,
        {
            "self_intro": "self_intro",
            "pro_qa": "pro_qa",
            "compre_qa": "compre_qa",
            "report": "report",
            "end": "completed",
        },
    )

    for node in ("self_intro", "pro_qa", "compre_qa", "report", "completed"):
        builder.add_edge(node, END)

    memory = MemorySaver()
    return builder.compile(checkpointer=memory)
```

### 为什么用 `set_conditional_entry_point`

每一轮都是一个新的图调用，输入只有 `user_query`。真正决定“这一轮去哪”的是**上一轮保存下来的 `current_phase`**。条件入口正好表达这个语义：

```text
新的一次 ainvoke/astream
   │
   ├─ 读 checkpoint 里的 current_phase
   ├─ route_phase 决定入口节点
   └─ 执行一个节点后直接 END
```

不是“一个节点连到下一个节点”的长流程，而是“每次调用只执行一个节点”的回合制图。这样音频层的“一轮”和图层的“一次调用”一一对应。

### 为什么每个节点都连 `END`

每次调用只处理当前回合，阶段推进通过 state 里的 `current_phase` 在**下一次调用**生效。不要在同一个调用里连续执行多个节点——那会让一次语音回合产生多段 TTS，节奏不可控。

### 全局 `interview_graph`

```python
interview_graph = create_interview_graph()
```

模块导入时编译一次，`MemorySaver` 在进程内共享。所有 thread 通过 `config={"configurable": {"thread_id": ...}}` 隔离。

---

## 4.8 一次多轮流程（用测试驱动）

`tests/test_graph_flow.py` 用 `FakeListChatModel` 或 mock 驱动完整流程：

```python
graph = create_interview_graph()
config = {"configurable": {"thread_id": "flow-test-1"}}

state = await graph.ainvoke({
    "user_query": "各位老师好，我本科毕业于某某大学软件工程专业……"
}, config)
assert state["current_phase"] == "pro_qa"

state = await graph.ainvoke({"user_query": "好的老师，我准备好了。"}, config)
assert state["question_pro"] == "请解释进程与线程的区别。"

state = await graph.ainvoke({"user_query": "进程是资源分配单位……"}, config)
assert state["current_phase"] == "compre_qa"
...
```

测试替身的关键是：**按系统提示词返回固定答案**。

```python
def _fake_llm_response(messages):
    system_prompt = messages[0].content
    replies = {
        INTRO_JUDGE_SYSTEM: "合格",
        PRO_QUESTION_SYSTEM: "请解释进程与线程的区别。",
        PRO_JUDGE_SYSTEM: "不需要",
        ...
        FINAL_REPORT_SYSTEM: REPORT_JSON,
    }
    return AIMessage(content=replies[system_prompt])
```

这样不用网络、不用 Key，也能验证：

- 阶段是否按预期推进；
- real 模式是否真的问 3 道题；
- 最后一题答完是否同轮出报告；
- 完成后再说一句是否回“面试已经结束”。

---

## 4.9 函数索引

| 函数/常量 | 作用 | 什么时候改它 |
| --- | --- | --- |
| `InterviewState` | 状态字段 | 加新维度/记录时 |
| `*_SYSTEM` | 提示词 | 调整人设、题风、输出格式 |
| `_get_llm` | 带缓存/超时的客户端 | 换模型、调超时/重试 |
| `_needs_followup` | 解析判分输出 | 判分词表变化时 |
| `_question_prompt` | 拼“已问问题”上下文 | 防重复策略变化时 |
| `_remember_question` | 累积已问题目 | 同上 |
| `_interview_settings` | 解析模式预算 | 加新预算项时 |
| `_reply_text` | 流式/非流式回复 | 换流式协议时 |
| `node_self_intro` | 自我介绍判定/重试 | 改合格标准时 |
| `node_pro_qa` | 专业出题/追问/推进 | 改专业环节逻辑时 |
| `node_compre_qa` | 综合出题/追问/报告 | 改综合环节逻辑时 |
| `_generate_report` | 生成并解析报告 | 改评分维度时 |
| `_build_report_payload` | 规范化 JSON | 同上 |
| `_format_report_text` | 纯文本成绩单 | 改展示格式时 |
| `node_report` | 报告路由的薄包装 | 加“重生成报告”入口时 |
| `node_completed` | 结束后的提醒 | 改结束语时 |
| `route_phase` | 入口路由 | 加新阶段时 |
| `create_interview_graph` | 编译图 | 加节点/边时 |
| `interview_graph` | 全局图实例 | 换 checkpointer 时 |

---

## 4.10 常见坑

| 坑 | 表现 | 原因 | 修法 |
| --- | --- | --- | --- |
| 忘记返回 `reply` | 重复播上一句 | state 增量合并 | 每个分支都显式返回 `reply` |
| 忘记返回 `raw_scores` | 报告缺记录 | 覆盖而非追加 | 每个分支带上 `archive` |
| 在节点外调用 `_reply_text` | `get_stream_writer` 报错/无输出 | 不在图运行上下文 | 只在节点内调用 |
| 判分词匹配太宽 | “不需要”被当成“需要” | 直接 `in "需要"` | 先排除“不需要” |
| 模式常量两处维护 | token 接受、图不认识 | 重复定义 | 用 `interview_modes.py` |
| 完成后报告重复弹 | 每次说话都弹窗 | checkpoint 保留 `report_data` | 适配器只发一次 |
| 题目重复 | real 模式三题很像 | 出题提示词没有历史 | `asked_questions` |

---

## 4.11 检查点

1. 为什么每个节点执行完都连 `END`，而不是连下一个阶段节点？
2. 如果考生回答完专业题，判分说“需要追问”，但追问次数已用尽，会发生什么？
3. 为什么成绩单缺失 `total_score` 时不使用默认 85 分？
4. `asked_questions` 在哪个时机被重置？为什么？
5. 如何只用测试驱动，验证 real 模式确实问了 3 道专业题？

下一章：把这个状态机接到 LiveKit 的 LLM 接口上。

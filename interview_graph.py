import json
import logging
import re
from functools import lru_cache
from typing import Any, Literal, TypedDict
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from config import config, delivery_metrics_enabled, resolve_api_key
from interview_modes import DEFAULT_MODE, normalize_mode as normalize_interview_mode
from learner_store import (
    PRO_TOPICS,
    SOFT_TOPICS,
    TOPIC_LABELS,
    get_store,
    normalize_topic,
    normalize_verdict,
    parse_focus_topics,
)
from perception.report_text import build_report_user_content, delivery_report

logger = logging.getLogger("interview-langgraph")

# ==========================================
# 面试状态定义
# ==========================================
class InterviewState(TypedDict):
    # 阶段："self_intro" | "pro_qa" | "compre_qa" | "report" | "completed"
    current_phase: str
    user_query: str
    reply: str
    follow_up_count: int
    question_pro: str
    question_compre: str
    is_answering: bool
    intro_attempts: int
    # 每个阶段的题目额度（"quick" 问 1 道，"real" 问多道）。
    interview_mode: str
    questions_per_phase: int
    questions_asked: int
    max_follow_ups: int
    max_intro_attempts: int
    # 已经问过的主题目，供下一题提示词避免重复。需要手动累积，因为
    # LangGraph 默认通道语义会整段替换普通 str 字段。
    asked_questions: str
    raw_scores: str
    final_report: str
    # 结构化成绩单，供前端成绩卡使用。
    report_data: dict[str, Any]
    # 各轮表达特征。出题和追问不读它。
    delivery_turns: list
    # 跨场长期记忆：考生档案 id、本场会话 id、加强方向与开场画像摘要。
    learner_id: str
    session_id: str
    focus_topics: str
    profile_snapshot: str
    # 本场结构化评答记录（每行一条 JSON），终局写回知识库。
    turn_tags: str


# ==========================================
# 提示词
# ==========================================
INTRO_JUDGE_SYSTEM = (
    "身份：你是考研复试自我介绍的内容检查员。\n"
    "性格：只做判断，不解释，不给建议。\n"
    "任务：两项都有才输出合格：本科背景，以及一段具体经历或报考动机。缺任何一项就输出不合格。不要凭出现过某个词就判合格。字数是否够长由系统另外判断，你不要因为字数下结论。\n"
    "输出：只能是「合格」或「不合格」。"
)

UNQUALIFY_SYSTEM = (
    "身份：你是考研复试的面试官，正在听自我介绍。\n"
    "性格：严谨、温和。先简短肯定，再只补问缺的那一项。\n"
    "任务：自我介绍没过。缺的只可能是这三项里的某一项：本科背景、一段具体经历、报考动机。只补问缺的那一项。\n"
    "输出：纯台词，不要旁白，不要「面试官：」前缀。大约 100 字。"
)

PRO_QUESTION_SYSTEM = (
    "身份：你是计算机/软件工程考研复试的专业课面试官。\n"
    "性格：严谨。一次只问一件事。\n"
    "任务：从数据结构、操作系统、计算机网络、系统设计里出题。每次只问一个能口头答完的机制、取舍或故障。不考名词背诵。不重复已经问过的问题。"
    "若提示里给出了优先加强方向，本场至少一半主题目对准这些方向，其余可覆盖其他范围。\n"
    "输出：纯台词，不要前缀，不要泄题，不要连环问。50 字以内。"
)

_PRO_TOPIC_LIST = "、".join(PRO_TOPICS)
PRO_JUDGE_SYSTEM = (
    "身份：你是计算机/软件工程考研复试的专业课评判。\n"
    "性格：严谨。只做判断，不解释。\n"
    "任务：判断是否需要追问，并给本题打一个知识点标签和掌握判定。\n"
    f"topic 只能从这些里选一个：{_PRO_TOPIC_LIST}。\n"
    "verdict：strong=覆盖核心且清楚；ok=基本到位但可更好；weak=简略/表面/漏原理；"
    "unknown=考生明确说完全不会。\n"
    "followup：回答过于简略、只停留在表面概念、或漏了核心原理时为「需要」；"
    "已经覆盖核心，或 verdict 为 unknown 时为「不需要」。\n"
    "输出：只能是合法 JSON，不要其它文字：\n"
    '{"followup":"需要|不需要","topic":"os.memory","verdict":"weak|ok|strong|unknown"}'
)

PRO_FOLLOWUP_SYSTEM = (
    "身份：你是计算机/软件工程考研复试的专业课面试官。\n"
    "性格：严谨，更紧，只对准一个缺口。\n"
    "任务：从考生刚才的回答里只挑一个缺口追问，原理、实现或边界里三选一。\n"
    "输出：纯台词，不要前缀，不要泄题，不要连环问。50 字以内。"
)

TRANSITION_TO_COMPRE_SYSTEM = (
    "身份：你是计算机/软件工程考研复试的专业课面试官。\n"
    "性格：严谨、温和。\n"
    "任务：专业课问答已经结束。用一句话告诉考生这一问结束了，接下来进入综合素质。\n"
    "输出：纯台词。40 字以内。"
)

COMPRE_QUESTION_SYSTEM = (
    "身份：你是考研复试的面试官。\n"
    "性格：温和、严谨。一次只问一件事。\n"
    "任务：从读研动机、职业规划、科研或竞赛经历、团队合作冲突、抗压里挑一个，问一个具体场景。不评价考生的性格。"
    "若提示里给出了优先加强方向，尽量对准这些方向出题，仍不重复已问。\n"
    "输出：纯台词，不要前缀，不要连环问。60 字以内。"
)

_SOFT_TOPIC_LIST = "、".join(SOFT_TOPICS)
COMPRE_JUDGE_SYSTEM = (
    "身份：你是考研复试综合素质的评判。\n"
    "性格：严谨。只做判断，不解释。\n"
    "任务：判断是否需要追问，并给本题打一个综合素质标签和掌握判定。\n"
    f"topic 只能从这些里选一个：{_SOFT_TOPIC_LIST}。\n"
    "verdict：strong=有具体事例且说清；ok=基本到位；weak=简略/空洞；"
    "unknown=考生明确没有相关经历。\n"
    "followup：简略、空洞或没有具体事例时为「需要」；已有事例说清，或 verdict 为 unknown 时为「不需要」。\n"
    "输出：只能是合法 JSON，不要其它文字：\n"
    '{"followup":"需要|不需要","topic":"soft.team","verdict":"weak|ok|strong|unknown"}'
)

COMPRE_FOLLOWUP_SYSTEM = (
    "身份：你是考研复试的面试官。\n"
    "性格：温和、严谨。只对准一个缺口。\n"
    "任务：考生刚才用了空泛的词。请他用一个具体事例把那个词说实。\n"
    "输出：纯台词，不要前缀，不要连环问。50 字以内。"
)

FINAL_REPORT_SYSTEM = (
    "身份：你是高校研究生招生委员会的复试组长。\n"
    "性格：客观。只根据记录下结论。\n"
    "任务：根据面试全程记录给出成绩单。三个维度各看各的：专业知识看专业题答得是否清楚；表达逻辑看意思有没有说清楚；综合素质看综合题有没有具体事例。不加分数档。\n"
    "输出：只能是合法 JSON，字段名保持如下：\n"
    "{\n"
    '  "total_score": 85,\n'
    '  "scores": {\n'
    '    "profession_know": 82,\n'
    '    "exp_logic": 88,\n'
    '    "comper_quality": 85\n'
    "  },\n"
    '  "core_weakness": "核心不足（50字内）",\n'
    '  "improvement_tips": "改进建议（100字内）",\n'
    '  "interviewer_closing": "直接对考生说的结束语"\n'
    "}"
)

_DELIVERY_SYSTEM_ADDENDUM = (
    "\n表达特征只供表达逻辑参考。\n"
    "若用户消息附有表达特征，只允许它影响 scores.exp_logic 和 exp_logic_note。\n"
    "专业知识（scores.profession_know）和综合素质（scores.comper_quality）不得引用表达特征。\n"
    "核心不足（core_weakness）和改进建议（improvement_tips）也不得引用。\n"
    "total_score 仍综合三个维度给出，不要因为表达特征另设加减分。\n"
    "禁止把表达特征写成紧张、自信、情绪、性格或能力。\n"
    "在 JSON 中增加 exp_logic_note：一两句表达说明，只写说法方式。没有可靠表达特征时为空字符串。\n"
)


def report_system_prompt(enabled: bool) -> str:
    """关掉表达特征时，系统提示词与原来的纯文字成绩单相同。"""
    if not enabled:
        return FINAL_REPORT_SYSTEM
    return FINAL_REPORT_SYSTEM + _DELIVERY_SYSTEM_ADDENDUM


@lru_cache(maxsize=8)
def _get_llm(temperature: float = 0.5) -> ChatOpenAI:
    """按温度返回缓存的客户端。

    每次调用都新建 ``ChatOpenAI`` 会重复配置，超时和重试也容易在各调用点
    之间漂移。客户端可以安全复用，因为每个节点只对同一端点发起无状态的
    ``ainvoke`` / ``astream`` 请求。
    """
    return ChatOpenAI(
        model=config.llm_model,
        api_key=resolve_api_key(config.llm_api_key),
        base_url=config.llm_base_url,
        temperature=temperature,
        timeout=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )


def _question_prompt(base: str, state: InterviewState) -> str:
    """把已经问过的问题、跨场画像与加强方向追加到出题提示词后面。"""
    parts = [base]
    asked = str(state.get("asked_questions", "") or "").strip()
    if asked:
        parts.append(f"已经问过的问题（不要重复，也不要换汤不换药）：\n{asked}")
    snapshot = str(state.get("profile_snapshot", "") or "").strip()
    if snapshot:
        parts.append(snapshot)
    focus = parse_focus_topics(state.get("focus_topics"))
    if focus:
        labels = [TOPIC_LABELS.get(t, t) for t in focus]
        parts.append("本场必须优先加强：" + "、".join(labels))
    return "\n\n".join(parts)


def _remember_question(state: InterviewState, question: str) -> str:
    return str(state.get("asked_questions", "") or "") + f"- {question}\n"


def _needs_followup(judge_text: str) -> bool:
    """把评判模型的自由文本归一成是否追问的决定。

    评判提示词要求只输出“需要”或“不需要”；模型仍偶尔会包上标点或多余文字，
    因此先匹配否定说法。结构化 JSON 走 ``_parse_judge_payload``。
    """
    normalized = judge_text.strip()
    return "需要" in normalized and "不需要" not in normalized


def _parse_judge_payload(
    raw: str,
    *,
    allowed_topics: frozenset[str] | set[str],
    default_topic: str,
) -> dict[str, Any]:
    """解析评答 JSON；失败时回退到旧的「需要/不需要」文本约定。"""
    text = str(raw or "").strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    data: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except (TypeError, ValueError):
        data = None

    if data is None:
        followup = "需要" if _needs_followup(text) else "不需要"
        return {
            "followup": followup,
            "topic": default_topic,
            "verdict": "weak" if followup == "需要" else "ok",
        }

    followup_raw = str(data.get("followup") or "").strip()
    if "不需要" in followup_raw:
        followup = "不需要"
    elif "需要" in followup_raw:
        followup = "需要"
    else:
        followup = "需要" if _needs_followup(followup_raw or text) else "不需要"

    topic = normalize_topic(str(data.get("topic") or ""))
    if topic not in allowed_topics:
        topic = default_topic if default_topic in allowed_topics else next(iter(allowed_topics))

    verdict = normalize_verdict(str(data.get("verdict") or ""))
    if followup == "需要" and verdict == "ok":
        verdict = "weak"
    if verdict == "unknown":
        followup = "不需要"
    return {"followup": followup, "topic": topic, "verdict": verdict}


def _remember_turn_tag(
    state: InterviewState,
    *,
    question: str,
    topic: str,
    verdict: str,
    followup: str,
) -> str:
    record = {
        "question": str(question or "").strip(),
        "topic": topic,
        "verdict": verdict,
        "followup": followup,
    }
    line = json.dumps(record, ensure_ascii=False)
    prior = str(state.get("turn_tags", "") or "").rstrip()
    if prior:
        return prior + "\n" + line
    return line


def _parse_turn_tags(raw: str | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _weak_topics_from_turns(records: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in records:
        topic = normalize_topic(str(item.get("topic") or ""))
        verdict = normalize_verdict(str(item.get("verdict") or ""))
        if topic and verdict in {"weak", "unknown"} and topic not in out:
            out.append(topic)
    return out


# ==========================================
# 面试模式
# ==========================================
# "quick"：每个阶段一道主题目，节奏紧凑，适合演示录制。
# "real"：每个阶段多道主题目，并有更多追问，接近正常时长的模拟面试。
# 浏览器在加入前选择模式，经 LiveKit 令牌元数据传给智能体。
INTERVIEW_MODES: dict[str, dict[str, Any]] = {
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


def _interview_settings(state: InterviewState) -> dict[str, int]:
    """用状态里的覆盖值加上模式默认值，解析每个阶段的额度。"""
    mode = normalize_interview_mode(state.get("interview_mode") or config.interview_mode)
    defaults = INTERVIEW_MODES[mode]
    settings: dict[str, int] = {}
    for key in ("questions_per_phase", "max_follow_ups", "max_intro_attempts"):
        override = state.get(key)  # type: ignore[literal-required]
        try:
            value = int(override) if override is not None else int(defaults[key])
        except (TypeError, ValueError):
            value = int(defaults[key])
        settings[key] = max(0, value)
    return settings


async def _reply_text(llm: Any, messages: list[Any]) -> str:
    """生成口头回复；模型支持时把 token 流式推给界面。

    在 LangGraph 运行中，自定义流写入器把每个 token 转给 LLM 适配器，
    语音合成可以从第一个 token 开始，而不必等完整回复。单元测试用的
    替身不是 ``BaseChatModel`` 实例，会回退到普通的 ``ainvoke``。
    """
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


# ==========================================
# 图节点
# ==========================================
async def node_self_intro(state: InterviewState) -> dict:
    query = state.get("user_query", "").strip()
    attempts = int(state.get("intro_attempts", 0) or 0)
    settings = _interview_settings(state)
    logger.info(
        "[LangGraph] Processing self_intro, query length=%d, attempt=%d, mode=%s",
        len(query),
        attempts + 1,
        state.get("interview_mode") or config.interview_mode,
    )

    llm = _get_llm(temperature=0.1)
    judge_res = await llm.ainvoke([
        SystemMessage(content=INTRO_JUDGE_SYSTEM),
        HumanMessage(content=f"考生自我介绍：\n{query}"),
    ])
    result_text = judge_res.content.strip()

    # 字数只在这里判断。提示词只看本科背景，以及一段具体经历或报考动机。
    too_short = len(query) < config.min_self_intro_chars
    unqualified = "不合格" in result_text or too_short

    if unqualified and attempts < settings["max_intro_attempts"]:
        logger.info("[LangGraph] Self intro unqualified, prompting user to elaborate")
        tutor_llm = _get_llm(temperature=0.5)
        reply_text = await _reply_text(
            tutor_llm,
            [
                SystemMessage(content=UNQUALIFY_SYSTEM),
                HumanMessage(content=f"考生的原始自我介绍内容如下：{query}"),
            ],
        )
        return {
            "reply": reply_text,
            "current_phase": "self_intro",
            "is_answering": True,
            "intro_attempts": attempts + 1,
            "raw_scores": state.get("raw_scores", "") + f"【自我介绍】\n{query}\n",
        }

    if unqualified:
        # 重试额度用尽：仍然往下走，保证面试总能结束。
        logger.info("[LangGraph] Self intro retry budget exhausted, forcing transition to pro_qa")
        return {
            "reply": "好的，自我介绍部分我们先到这里。接下来进入专业知识问答环节，请听题。",
            "current_phase": "pro_qa",
            "is_answering": False,
            "intro_attempts": attempts,
            "questions_asked": 0,
            "asked_questions": "",
            "raw_scores": state.get("raw_scores", "") + f"【自我介绍(达到重试上限)】\n{query}\n",
        }

    logger.info("[LangGraph] Self intro qualified, transitioning to pro_qa")
    return {
        "reply": "很好，自我介绍通过。接下来我们正式进入专业知识问答环节，请准备好。",
        "current_phase": "pro_qa",
        "is_answering": False,
        "intro_attempts": attempts,
        "questions_asked": 0,
        "asked_questions": "",
        "raw_scores": state.get("raw_scores", "") + f"【自我介绍(合格)】\n{query}\n",
    }


async def node_pro_qa(state: InterviewState) -> dict:
    query = state.get("user_query", "").strip()
    is_answering = state.get("is_answering", False)
    follow_up_count = int(state.get("follow_up_count", 0) or 0)
    current_q = state.get("question_pro", "")
    questions_asked = int(state.get("questions_asked", 0) or 0)
    settings = _interview_settings(state)

    logger.info(
        "[LangGraph] Processing pro_qa, is_answering=%s, follow_up_count=%d, questions_asked=%d/%d",
        is_answering,
        follow_up_count,
        questions_asked,
        settings["questions_per_phase"],
    )
    llm = _get_llm(temperature=0.6)

    # 没有待答题目：提出本阶段当前的主题目。
    if not is_answering or not current_q:
        prompt = (
            "请出一道题：只问一个能口头答完的机制、取舍或故障。"
            if questions_asked == 0
            else "请再出一道题，不要重复已经问过的。仍只问一个机制、取舍或故障。"
        )
        question = await _reply_text(
            llm,
            [
                SystemMessage(content=PRO_QUESTION_SYSTEM),
                HumanMessage(content=_question_prompt(prompt, state)),
            ],
        )
        return {
            "question_pro": question,
            "reply": question,
            "is_answering": True,
            "follow_up_count": 0,
            "questions_asked": questions_asked + 1,
            "asked_questions": _remember_question(state, question),
        }

    # 考生已经回答了这道题
    archive = state.get("raw_scores", "") + f"【专业提问】\n{current_q}\n【考生回答】\n{query}\n"

    # 判断是否需要追问，并打知识点标签
    judge_llm = _get_llm(temperature=0.1)
    judge_res = await judge_llm.ainvoke([
        SystemMessage(content=PRO_JUDGE_SYSTEM),
        HumanMessage(content=f"【考官问题】{current_q}\n【考生回答】{query}"),
    ])
    judgment = _parse_judge_payload(
        str(judge_res.content),
        allowed_topics=frozenset(PRO_TOPICS),
        default_topic="se.design",
    )
    need_followup = judgment["followup"] == "需要"
    turn_tags = _remember_turn_tag(
        state,
        question=current_q,
        topic=judgment["topic"],
        verdict=judgment["verdict"],
        followup=judgment["followup"],
    )
    tagged_state = {**state, "turn_tags": turn_tags}

    if need_followup and follow_up_count < settings["max_follow_ups"]:
        logger.info("[LangGraph] Pro QA triggering follow-up")
        followup_q = await _reply_text(
            _get_llm(temperature=0.4),
            [
                SystemMessage(content=PRO_FOLLOWUP_SYSTEM),
                HumanMessage(content=f"【原问题】{current_q}\n【考生回答】{query}"),
            ],
        )
        return {
            "question_pro": followup_q,
            "reply": followup_q,
            "follow_up_count": follow_up_count + 1,
            "is_answering": True,
            "questions_asked": questions_asked,
            "asked_questions": _remember_question(state, followup_q),
            "raw_scores": archive,
            "turn_tags": turn_tags,
        }

    if questions_asked < settings["questions_per_phase"]:
        logger.info(
            "[LangGraph] Pro QA asking next question (%d/%d)",
            questions_asked + 1,
            settings["questions_per_phase"],
        )
        next_question = await _reply_text(
            llm,
            [
                SystemMessage(content=PRO_QUESTION_SYSTEM),
                HumanMessage(
                    content=_question_prompt(
                        "请再出一道题，不要重复已经问过的。仍只问一个机制、取舍或故障。",
                        tagged_state,
                    )
                ),
            ],
        )
        return {
            "question_pro": next_question,
            "reply": next_question,
            "is_answering": True,
            "follow_up_count": 0,
            "questions_asked": questions_asked + 1,
            "asked_questions": _remember_question(state, next_question),
            "raw_scores": archive,
            "turn_tags": turn_tags,
        }

    logger.info("[LangGraph] Pro QA finished, moving to compre_qa")
    transition_msg = await _reply_text(
        _get_llm(temperature=0.3),
        [
            SystemMessage(content=TRANSITION_TO_COMPRE_SYSTEM),
            HumanMessage(content="告知考生专业知识问答结束，准备进入综合素质问答。"),
        ],
    )
    return {
        "reply": f"{transition_msg} 接下来我想了解一些你的综合经历与个人规划。",
        "current_phase": "compre_qa",
        "is_answering": False,
        "follow_up_count": 0,
        "questions_asked": 0,
        "asked_questions": "",
        "raw_scores": archive,
        "turn_tags": turn_tags,
    }


async def node_compre_qa(state: InterviewState) -> dict:
    query = state.get("user_query", "").strip()
    is_answering = state.get("is_answering", False)
    follow_up_count = int(state.get("follow_up_count", 0) or 0)
    current_q = state.get("question_compre", "")
    questions_asked = int(state.get("questions_asked", 0) or 0)
    settings = _interview_settings(state)

    logger.info(
        "[LangGraph] Processing compre_qa, is_answering=%s, follow_up_count=%d, questions_asked=%d/%d",
        is_answering,
        follow_up_count,
        questions_asked,
        settings["questions_per_phase"],
    )
    llm = _get_llm(temperature=0.7)

    if not is_answering or not current_q:
        prompt = (
            "从读研动机、职业规划、科研或竞赛经历、团队合作冲突、抗压里挑一个，问一个具体场景。"
            if questions_asked == 0
            else "再挑一个还没问过的话题，问一个具体场景。不评价考生的性格。"
        )
        question = await _reply_text(
            llm,
            [
                SystemMessage(content=COMPRE_QUESTION_SYSTEM),
                HumanMessage(content=_question_prompt(prompt, state)),
            ],
        )
        return {
            "question_compre": question,
            "reply": question,
            "is_answering": True,
            "follow_up_count": 0,
            "questions_asked": questions_asked + 1,
            "asked_questions": _remember_question(state, question),
        }

    # 考生已经回答了综合素质题
    archive = state.get("raw_scores", "") + f"【综合提问】\n{current_q}\n【考生回答】\n{query}\n"

    judge_llm = _get_llm(temperature=0.1)
    judge_res = await judge_llm.ainvoke([
        SystemMessage(content=COMPRE_JUDGE_SYSTEM),
        HumanMessage(content=f"【综合问题】{current_q}\n【考生回答】{query}"),
    ])
    judgment = _parse_judge_payload(
        str(judge_res.content),
        allowed_topics=frozenset(SOFT_TOPICS),
        default_topic="soft.motive",
    )
    need_followup = judgment["followup"] == "需要"
    turn_tags = _remember_turn_tag(
        state,
        question=current_q,
        topic=judgment["topic"],
        verdict=judgment["verdict"],
        followup=judgment["followup"],
    )
    tagged_state = {**state, "turn_tags": turn_tags, "raw_scores": archive}

    if need_followup and follow_up_count < settings["max_follow_ups"]:
        logger.info("[LangGraph] Compre QA triggering follow-up")
        followup_q = await _reply_text(
            llm,
            [
                SystemMessage(content=COMPRE_FOLLOWUP_SYSTEM),
                HumanMessage(content=f"【提问】{current_q}\n【回答】{query}"),
            ],
        )
        return {
            "question_compre": followup_q,
            "reply": followup_q,
            "follow_up_count": follow_up_count + 1,
            "is_answering": True,
            "questions_asked": questions_asked,
            "asked_questions": _remember_question(state, followup_q),
            "raw_scores": archive,
            "turn_tags": turn_tags,
        }

    if questions_asked < settings["questions_per_phase"]:
        logger.info(
            "[LangGraph] Compre QA asking next question (%d/%d)",
            questions_asked + 1,
            settings["questions_per_phase"],
        )
        next_question = await _reply_text(
            llm,
            [
                SystemMessage(content=COMPRE_QUESTION_SYSTEM),
                HumanMessage(
                    content=_question_prompt(
                        "再挑一个还没问过的话题，问一个具体场景。不评价考生的性格。",
                        tagged_state,
                    )
                ),
            ],
        )
        return {
            "question_compre": next_question,
            "reply": next_question,
            "is_answering": True,
            "follow_up_count": 0,
            "questions_asked": questions_asked + 1,
            "asked_questions": _remember_question(state, next_question),
            "raw_scores": archive,
            "turn_tags": turn_tags,
        }

    # 在同一轮里生成成绩单。若本轮停在 "report" 阶段，前端会再等一句
    # 用户发言，而面试并没有理由再要这句话。
    logger.info("[LangGraph] Compre QA finished, generating final report in this turn")
    report_result = await _generate_report(tagged_state)
    report_result["raw_scores"] = archive
    report_result["turn_tags"] = turn_tags
    return report_result


DEFAULT_WEAKNESS = "专业深度需继续拓展"
DEFAULT_TIPS = "建议巩固底层原理并增加实际项目实践"
DEFAULT_CLOSING = "同学，今天的面试到这里就圆满结束了。感谢你的参与，结果请等待官方通知。"


def _clamp_score(value: Any, default: int) -> int:
    """把模型给出的分数收成 [0, 100] 内的整数。"""
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, score))


def _build_report_payload(data: dict[str, Any]) -> dict[str, Any]:
    """把大模型的 JSON 成绩单整理成前端使用的结构。

    没有可用 ``total_score`` 的成绩单视为解析失败：展示一个编造的默认分，
    比承认成绩单失败更糟。
    """
    try:
        total_score = int(round(float(data.get("total_score"))))
    except (TypeError, ValueError):
        raise ValueError("report JSON is missing a usable total_score") from None
    total_score = max(0, min(100, total_score))

    raw_scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    weak_topics: list[str] = []
    raw_weak = data.get("weak_topics")
    if isinstance(raw_weak, list):
        for item in raw_weak:
            topic = normalize_topic(str(item))
            if topic and topic not in weak_topics:
                weak_topics.append(topic)
    return {
        "total_score": total_score,
        "scores": {
            "profession_know": _clamp_score(raw_scores.get("profession_know"), 80),
            "exp_logic": _clamp_score(raw_scores.get("exp_logic"), 85),
            "comper_quality": _clamp_score(raw_scores.get("comper_quality"), 85),
        },
        "core_weakness": str(data.get("core_weakness") or DEFAULT_WEAKNESS).strip(),
        "improvement_tips": str(data.get("improvement_tips") or DEFAULT_TIPS).strip(),
        "closing": str(data.get("interviewer_closing") or DEFAULT_CLOSING).strip(),
        "exp_logic_note": str(data.get("exp_logic_note") or "").strip(),
        "weak_topics": weak_topics,
    }


def _format_expression_note(note: str) -> str:
    text = str(note or "").strip()
    if not text:
        return ""
    return f"表达说明：{text}\n"


def _format_delivery_block(delivery: dict[str, Any] | None) -> str:
    if not isinstance(delivery, dict):
        return ""
    if not delivery.get("measured_turns"):
        count = int(delivery.get("missing_turns") or 0)
        detail = f"（{count} 轮）" if count else ""
        return f"表达特征：本场无可靠表达特征{detail}。\n\n"
    view = delivery.get("display") if isinstance(delivery.get("display"), dict) else {}
    lines = [
        "表达特征（只供表达逻辑参考，不是单独得分，也不表示情绪或性格）：",
        f"• 回答时长：{view.get('duration', '缺测')}",
        f"• 语速：{view.get('speech_rate', '缺测')}",
        f"• 停顿比例：{view.get('pause_ratio', '缺测')}",
        f"• 平均停顿：{view.get('mean_pause', '缺测')}",
        f"• 音高变化：{view.get('pitch_std', '缺测')}",
        f"• 音量：{view.get('intensity', '缺测')}",
        f"• 填充词比例：{view.get('filler_ratio', '缺测')}",
    ]
    missing = int(delivery.get("missing_turns") or 0)
    if missing:
        lines.append(f"• 缺测：{missing} 轮")
    return "\n".join(lines) + "\n\n"


def _format_report_text(report: dict[str, Any]) -> str:
    scores = report["scores"]
    return (
        "━━━━━━━━━━━━━━━\n"
        "🎓 考研复试模拟成绩单\n"
        "━━━━━━━━━━━━━━━\n"
        f"🏆 总评得分：{report['total_score']}分\n\n"
        "📊 分项维度：\n"
        f"• 专业知识：{scores['profession_know']}分\n"
        f"• 表达逻辑：{scores['exp_logic']}分\n"
        f"{_format_expression_note(report.get('exp_logic_note') or '')}"
        f"• 综合素质：{scores['comper_quality']}分\n\n"
        f"{_format_delivery_block(report.get('delivery'))}"
        "❌ 核心不足：\n"
        f"{report['core_weakness']}\n\n"
        "💡 改进建议：\n"
        f"{report['improvement_tips']}\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"{report['closing']}"
    )


def _persist_learner_memory(state: InterviewState, report: dict[str, Any]) -> list[str]:
    """把本场结构化评答写入个人知识库；失败不阻断成绩单。"""
    learner_id = str(state.get("learner_id") or "").strip()
    session_id = str(state.get("session_id") or "").strip()
    if not learner_id or not session_id:
        return list(report.get("weak_topics") or [])

    turn_records = _parse_turn_tags(state.get("turn_tags"))
    asked_items: list[tuple[str, str]] = []
    for item in turn_records:
        question = str(item.get("question") or "").strip()
        topic = normalize_topic(str(item.get("topic") or "")) or "se.design"
        if question:
            asked_items.append((question, topic))

    derived_weak = _weak_topics_from_turns(turn_records)
    merged = list(report.get("weak_topics") or [])
    for topic in derived_weak:
        if topic not in merged:
            merged.append(topic)
    report["weak_topics"] = merged

    try:
        return get_store().record_session(
            learner_id=learner_id,
            session_id=session_id,
            mode=str(state.get("interview_mode") or config.interview_mode),
            report=report,
            turn_records=turn_records,
            asked_items=asked_items,
        )
    except Exception:
        logger.warning(
            "[LangGraph] Failed to persist learner memory for %s",
            learner_id,
            exc_info=True,
        )
        return merged


async def _generate_report(state: InterviewState) -> dict:
    logger.info("[LangGraph] Generating final interview report")
    archive = state.get("raw_scores", "")

    enabled = delivery_metrics_enabled()
    turns = state.get("delivery_turns") or []
    llm = _get_llm(temperature=0.2)
    report_res = await llm.ainvoke([
        SystemMessage(content=report_system_prompt(enabled)),
        HumanMessage(content=build_report_user_content(archive, turns, enabled=enabled)),
    ])

    raw_text = report_res.content.strip()
    raw_text = re.sub(r"^```json\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text).strip()

    try:
        report = _build_report_payload(json.loads(raw_text))
    except Exception as e:
        logger.warning("[LangGraph] JSON report parsing failed, falling back to text: %s", e)
        fallback = {
            "reply": "面试已结束，非常感谢同学的配合，稍后可在后台查看详细报告。",
            "final_report": f"面试结束。\n{raw_text}",
            "report_data": {},
            "current_phase": "completed",
            "is_answering": False,
        }
        # 即便成绩单 JSON 失败，仍尽量落库本场标签
        _persist_learner_memory(
            state,
            {
                "total_score": 0,
                "core_weakness": DEFAULT_WEAKNESS,
                "improvement_tips": DEFAULT_TIPS,
                "weak_topics": _weak_topics_from_turns(_parse_turn_tags(state.get("turn_tags"))),
            },
        )
        return fallback

    if enabled:
        report["delivery"] = delivery_report(turns)

    weak_topics = _persist_learner_memory(state, report)
    report["weak_topics"] = weak_topics

    return {
        "reply": f"面试结束。总评得分 {report['total_score']} 分。{report['closing']}",
        "final_report": _format_report_text(report),
        "report_data": report,
        "current_phase": "completed",
        "is_answering": False,
    }


async def node_report(state: InterviewState) -> dict:
    return await _generate_report(state)


async def node_completed(state: InterviewState) -> dict:
    """处理面试已经结束后才到达的轮次。

    没有这个节点，图会原样返回上一状态，适配器就会把旧的结束语再念给考生。
    """
    logger.info("[LangGraph] Interview already completed, answering with reminder")
    return {
        "reply": "面试已经结束了。如需重新开始，请退出考场后重新进入。",
        "current_phase": "completed",
        "is_answering": False,
    }


# ==========================================
# 路由
# ==========================================
def route_phase(state: InterviewState) -> Literal["self_intro", "pro_qa", "compre_qa", "report", "end"]:
    phase = state.get("current_phase", "self_intro")
    if phase == "self_intro":
        return "self_intro"
    elif phase == "pro_qa":
        return "pro_qa"
    elif phase == "compre_qa":
        return "compre_qa"
    elif phase == "report":
        return "report"
    return "end"


# ==========================================
# 图构建
# ==========================================
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
            # 结束后的轮次进入提醒节点，而不是 END，
            # 这样考生不会再听到上一句结束语。
            "end": "completed",
        },
    )

    builder.add_edge("self_intro", END)
    builder.add_edge("pro_qa", END)
    builder.add_edge("compre_qa", END)
    builder.add_edge("report", END)
    builder.add_edge("completed", END)

    memory = MemorySaver()
    return builder.compile(checkpointer=memory)


interview_graph = create_interview_graph()

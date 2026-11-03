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

from config import config, delivery_metrics_enabled
from interview_modes import DEFAULT_MODE, normalize_mode as normalize_interview_mode
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


# ==========================================
# 提示词
# ==========================================
INTRO_JUDGE_SYSTEM = (
    "你是一个考研自我介绍的内容检查员。你的唯一任务是判断考生的自我介绍是否合格。\n"
    "【判断标准】\n"
    "- 少于50字或内容空洞 → 不合格\n"
    "- 包含“大学”、“经历”等词汇且内容基本完整 → 合格\n"
    "【严厉警告】输出必须仅为以下两个词之一：“合格”或“不合格”。严禁输出任何解释、标点符号或废话！"
)

UNQUALIFY_SYSTEM = (
    "# 维度一：角色定位 (Role)\n"
    "你是一名顶尖高校考研复试的专业面试官。你的态度严谨、温和且极其专业，善于通过引导发掘考生的闪光点，也擅长发现考生的不足。\n"
    "# 维度二：背景信息 (Background)\n"
    "在当前的考研复试“自我介绍”环节中，考生的初次自我介绍内容过于简略或缺失关键信息，无法全方位展示其能力。\n"
    "# 维度三：核心任务 (Core Task)\n"
    "深度分析考生提供的自我介绍文本，精准定位其缺失的关键核心要素（如：本科院校及专业背景、科研/竞赛/项目实践经历、报考本校本专业的动机等），并以面试官的口吻主动引导考生进行针对性补充。\n"
    "# 维度四：规则限制 (Rules & Constraints)\n"
    "1. 纯粹台词：输出内容必须 100% 是面试官对考生说的话，不要包含任何旁白、动作描写或“面试官：”前缀。\n"
    "2. 话术结构：简短肯定 -> 指出不足与期望 -> 发起具体追问补充。\n"
    "3. 总字数控制在 100 字左右，语气自然温和但有学术要求。"
)

PRO_QUESTION_SYSTEM = (
    "# 维度一：角色定位 (Role)\n"
    "你是一名顶尖高校计算机/软件工程考研复试的专业课面试官。你学术严谨、提问一针见血。\n"
    "# 维度二：核心任务 (Core Task)\n"
    "考生已完成自我介绍，正式进入专业知识问答环节。请提出一道核心专业课面试题（如数据结构、操作系统、计算机网络、系统设计等核心概念）。\n"
    "# 维度三：规则限制\n"
    "1. 每次必须且只能提出一道清晰具体的问题。\n"
    "2. 真人面试官直接提问，严禁废话、严禁泄题、严禁“问题一：”等前缀。\n"
    "3. 简短精炼，50字以内。"
)

PRO_JUDGE_SYSTEM = (
    "你是一个严谨的考研面试评价系统。你的唯一任务是比对考生的回答，判断是否需要对考生发起“追问”。\n"
    "【判定标准】\n"
    "- 输出“需要”：考生的回答过于简略、只答出表面概念缺乏深度、遗漏了核心原理，需要进一步挖掘。\n"
    "- 输出“不需要”：考生的回答已经非常完整准确，覆盖了核心知识点；或者考生明确表示完全不会。\n"
    "【致命警告】你的输出必须且只能是“需要”或“不需要”两个字或三个字。严禁输出任何标点符号或解释！"
)

PRO_FOLLOWUP_SYSTEM = (
    "# 维度一：角色定位 (Role)\n"
    "你是一名顶尖高校考研复试的专业课主考官。你思维敏锐、作风严厉，以“一针见血”著称。\n"
    "# 维度二：核心任务 (Core Task)\n"
    "针对考生回答中的模糊点或漏洞，提出一次简短有力的追问（探底原理、底层实现或边界条件）。\n"
    "# 维度三：规则限制\n"
    "1. 只能挑最关键的一个点发问，禁止连环提问，字数50字以内。\n"
    "2. 纯面试官台词，不包含前缀，不泄露答案。"
)

TRANSITION_TO_COMPRE_SYSTEM = (
    "你是一名顶尖高校考研复试的专业课主考官。\n"
    "专业课问答环节已告一段落，准备进入综合素质考察环节。\n"
    "请拟定一句简短的过渡台词（40字以内），告知考生该问题已结束，准备进行综合能力交流。语气严谨温和。"
)

COMPRE_QUESTION_SYSTEM = (
    "# 维度一：角色定位 (Role)\n"
    "你是一名顶尖高校考研复试的主考官。阅人无数，善于通过开放性问题洞察考生的真实性格、底层逻辑与抗压能力。\n"
    "# 维度二：核心任务 (Core Task)\n"
    "从【读研动机、职业规划、本科科研/竞赛经历、团队合作冲突、个人优缺点与抗压】中挑选 1 个话题，向考生提出一个具体的场景化问题。\n"
    "# 维度三：规则限制\n"
    "1. 每次最多只能问 1 个具体问题，不要连环提问。\n"
    "2. 语言自然亲切但有深度，60字以内，不带“考官：”前缀。"
)

COMPRE_JUDGE_SYSTEM = (
    "你是一个严谨的考研面试综合素质评估系统。你的任务是分析考生对软技能/综合类话题的回答，判断是否需要追问。\n"
    "【判定标准】\n"
    "- 输出“需要”：考生的回答过于简略敷衍、空洞乏味、缺乏真实事例细节，需要进一步深挖感悟。\n"
    "- 输出“不需要”：考生的回答已经非常充分、逻辑清晰且有事例支撑；或者考生无相关经历。\n"
    "【输出限制】必须且只能输出“需要”或“不需要”。严禁包含其他任何字符！"
)

COMPRE_FOLLOWUP_SYSTEM = (
    "# 维度一：角色定位 (Role)\n"
    "你是一名资深且富有洞察力的高校考研复试面试官。态度温和，善于从考生的回答中捕捉细节，考察其实际软实力。\n"
    "# 维度二：核心任务 (Core Task)\n"
    "针对考生刚才回答中空泛的词汇，要求其结合具体的事例、困难或细节来证明。\n"
    "# 维度三：规则限制\n"
    "单点追问，字数50字以内，纯面试官台词。"
)

FINAL_REPORT_SYSTEM = (
    "# 维度一：角色定位 (Role)\n"
    "你是一名高校研究生招生委员会的资深复试组长。评价眼光客观犀利，直击要害。\n"
    "# 维度二：核心任务 (Core Task)\n"
    "根据面试全程记录，给出最终评分报告。\n"
    "必须且只能输出合法的 JSON 格式：\n"
    "{\n"
    '  "total_score": 85,\n'
    '  "scores": {\n'
    '    "profession_know": 82,\n'
    '    "exp_logic": 88,\n'
    '    "comper_quality": 85\n'
    "  },\n"
    '  "core_weakness": "核心不足描述（50字内）",\n'
    '  "improvement_tips": "改进建议描述（100字内）",\n'
    '  "interviewer_closing": "面试官结语台词（直接对考生说的话，如感谢参加并告知面试结束）"\n'
    "}"
)

_DELIVERY_SYSTEM_ADDENDUM = (
    "\n# 表达特征\n"
    "若用户消息附有表达特征，只允许它影响 scores.exp_logic 和 exp_logic_note。\n"
    "scores.profession_know（专业知识）和 scores.comper_quality（综合素质）不得引用表达特征。\n"
    "core_weakness（核心不足）和 improvement_tips（改进建议）也不得引用。\n"
    "total_score 仍综合三个维度给出，不要因为表达特征另设加减分。\n"
    "禁止把表达特征写成紧张、自信、情绪、性格或能力。\n"
    "在 JSON 中增加 exp_logic_note：一两句话说明说法方式；没有可靠表达特征时为空字符串。\n"
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
        api_key=config.llm_api_key or "sk-dummy",
        base_url=config.llm_base_url,
        temperature=temperature,
        timeout=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )


def _question_prompt(base: str, state: InterviewState) -> str:
    """把已经问过的问题追加到出题提示词后面。"""
    asked = str(state.get("asked_questions", "") or "").strip()
    if not asked:
        return base
    return f"{base}\n\n已经问过的问题（不要重复，也不要换汤不换药）：\n{asked}"


def _remember_question(state: InterviewState, question: str) -> str:
    return str(state.get("asked_questions", "") or "") + f"- {question}\n"


def _needs_followup(judge_text: str) -> bool:
    """把评判模型的自由文本归一成是否追问的决定。

    评判提示词要求只输出“需要”或“不需要”；模型仍偶尔会包上标点或多余文字，
    因此先匹配否定说法。
    """
    normalized = judge_text.strip()
    return "需要" in normalized and "不需要" not in normalized


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

    # 提示词和代码必须使用同一道长度门槛，评判结论也不能被另一道不同的
    # 阈值悄悄覆盖。
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
            "请向考生提出第一道专业课面试题。"
            if questions_asked == 0
            else "请向考生提出下一道专业课面试题，不要重复之前问过的问题。"
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

    # 判断是否需要追问
    judge_llm = _get_llm(temperature=0.1)
    judge_res = await judge_llm.ainvoke([
        SystemMessage(content=PRO_JUDGE_SYSTEM),
        HumanMessage(content=f"【考官问题】{current_q}\n【考生回答】{query}"),
    ])
    need_followup = _needs_followup(judge_res.content)

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
                        "请向考生提出下一道专业课面试题，不要重复之前问过的问题。",
                        state,
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
            "提出一道关于综合素质/个人规划/团队协作的面试问题。"
            if questions_asked == 0
            else "请再提出一道主题不同的综合素质面试题，不要重复之前问过的话题。"
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
    need_followup = _needs_followup(judge_res.content)

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
                        "请再提出一道主题不同的综合素质面试题，不要重复之前问过的话题。",
                        state,
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
        }

    # 在同一轮里生成成绩单。若本轮停在 "report" 阶段，前端会再等一句
    # 用户发言，而面试并没有理由再要这句话。
    logger.info("[LangGraph] Compre QA finished, generating final report in this turn")
    report_result = await _generate_report({**state, "raw_scores": archive})
    report_result["raw_scores"] = archive
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
        return {
            "reply": "面试已结束，非常感谢同学的配合，稍后可在后台查看详细报告。",
            "final_report": f"面试结束。\n{raw_text}",
            "report_data": {},
            "current_phase": "completed",
            "is_answering": False,
        }

    if enabled:
        report["delivery"] = delivery_report(turns)

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

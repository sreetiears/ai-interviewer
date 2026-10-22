import json
import logging
import re
from typing import Literal, TypedDict
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from config import config

logger = logging.getLogger("interview-langgraph")

# ==========================================
# Interview State Definition
# ==========================================
class InterviewState(TypedDict):
    # Phase: "self_intro" | "pro_qa" | "compre_qa" | "report" | "completed"
    current_phase: str
    user_query: str
    reply: str
    follow_up_count: int
    question_pro: str
    question_compre: str
    is_answering: bool
    raw_scores: str
    final_report: str


# ==========================================
# Prompts
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


def _get_llm(temperature: float = 0.5) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.llm_model,
        api_key=config.llm_api_key or "sk-dummy",
        base_url=config.llm_base_url,
        temperature=temperature,
    )


# ==========================================
# Graph Nodes
# ==========================================
async def node_self_intro(state: InterviewState) -> dict:
    query = state.get("user_query", "").strip()
    logger.info("[LangGraph] Processing self_intro, query length=%d", len(query))

    llm = _get_llm(temperature=0.1)
    judge_res = await llm.ainvoke([
        SystemMessage(content=INTRO_JUDGE_SYSTEM),
        HumanMessage(content=f"考生自我介绍：\n{query}"),
    ])
    result_text = judge_res.content.strip()

    if "不合格" in result_text or len(query) < 40:
        logger.info("[LangGraph] Self intro unqualified, prompting user to elaborate")
        tutor_llm = _get_llm(temperature=0.5)
        reply_res = await tutor_llm.ainvoke([
            SystemMessage(content=UNQUALIFY_SYSTEM),
            HumanMessage(content=f"考生的原始自我介绍内容如下：{query}"),
        ])
        return {
            "reply": reply_res.content.strip(),
            "current_phase": "self_intro",
            "is_answering": True,
            "raw_scores": state.get("raw_scores", "") + f"【自我介绍】\n{query}\n",
        }
    else:
        logger.info("[LangGraph] Self intro qualified, transitioning to pro_qa")
        reply = "很好，自我介绍通过。接下来我们正式进入专业知识问答环节，请准备好。"
        return {
            "reply": reply,
            "current_phase": "pro_qa",
            "is_answering": False,
            "raw_scores": state.get("raw_scores", "") + f"【自我介绍(合格)】\n{query}\n",
        }


async def node_pro_qa(state: InterviewState) -> dict:
    query = state.get("user_query", "").strip()
    is_answering = state.get("is_answering", False)
    follow_up_count = state.get("follow_up_count", 0)
    current_q = state.get("question_pro", "")

    logger.info("[LangGraph] Processing pro_qa, is_answering=%s, follow_up_count=%d", is_answering, follow_up_count)
    llm = _get_llm(temperature=0.6)

    # If we haven't asked a question yet or just transitioned from intro
    if not is_answering or not current_q:
        q_res = await llm.ainvoke([
            SystemMessage(content=PRO_QUESTION_SYSTEM),
            HumanMessage(content="请向考生提出第一道专业课面试题。"),
        ])
        question = q_res.content.strip()
        return {
            "question_pro": question,
            "reply": question,
            "is_answering": True,
            "follow_up_count": 0,
        }

    # Candidate has answered the question
    archive = state.get("raw_scores", "") + f"【专业提问】\n{current_q}\n【考生回答】\n{query}\n"

    # Judge if follow-up is needed
    judge_llm = _get_llm(temperature=0.1)
    judge_res = await judge_llm.ainvoke([
        SystemMessage(content=PRO_JUDGE_SYSTEM),
        HumanMessage(content=f"【考官问题】{current_q}\n【考生回答】{query}"),
    ])
    need_followup = "需要" in judge_res.content.strip() and "不需要" not in judge_res.content.strip()

    if need_followup and follow_up_count < config.max_follow_ups:
        logger.info("[LangGraph] Pro QA triggering follow-up")
        fu_llm = _get_llm(temperature=0.4)
        fu_res = await fu_llm.ainvoke([
            SystemMessage(content=PRO_FOLLOWUP_SYSTEM),
            HumanMessage(content=f"【原问题】{current_q}\n【考生回答】{query}"),
        ])
        followup_q = fu_res.content.strip()
        return {
            "question_pro": followup_q,
            "reply": followup_q,
            "follow_up_count": follow_up_count + 1,
            "is_answering": True,
            "raw_scores": archive,
        }
    else:
        # Move to comprehensive phase
        logger.info("[LangGraph] Pro QA finished, moving to compre_qa")
        trans_llm = _get_llm(temperature=0.3)
        trans_res = await trans_llm.ainvoke([
            SystemMessage(content=TRANSITION_TO_COMPRE_SYSTEM),
            HumanMessage(content="告知考生专业知识问答结束，准备进入综合素质问答。"),
        ])
        transition_msg = trans_res.content.strip()
        return {
            "reply": f"{transition_msg} 接下来我想了解一些你的综合经历与个人规划。",
            "current_phase": "compre_qa",
            "is_answering": False,
            "follow_up_count": 0,
            "raw_scores": archive,
        }


async def node_compre_qa(state: InterviewState) -> dict:
    query = state.get("user_query", "").strip()
    is_answering = state.get("is_answering", False)
    follow_up_count = state.get("follow_up_count", 0)
    current_q = state.get("question_compre", "")

    logger.info("[LangGraph] Processing compre_qa, is_answering=%s, follow_up_count=%d", is_answering, follow_up_count)
    llm = _get_llm(temperature=0.7)

    if not is_answering or not current_q:
        q_res = await llm.ainvoke([
            SystemMessage(content=COMPRE_QUESTION_SYSTEM),
            HumanMessage(content="提出一道关于综合素质/个人规划/团队协作的面试问题。"),
        ])
        question = q_res.content.strip()
        return {
            "question_compre": question,
            "reply": question,
            "is_answering": True,
            "follow_up_count": 0,
        }

    # Candidate answered comprehensive question
    archive = state.get("raw_scores", "") + f"【综合提问】\n{current_q}\n【考生回答】\n{query}\n"

    judge_llm = _get_llm(temperature=0.1)
    judge_res = await judge_llm.ainvoke([
        SystemMessage(content=COMPRE_JUDGE_SYSTEM),
        HumanMessage(content=f"【综合问题】{current_q}\n【考生回答】{query}"),
    ])
    need_followup = "需要" in judge_res.content.strip() and "不需要" not in judge_res.content.strip()

    if need_followup and follow_up_count < config.max_follow_ups:
        logger.info("[LangGraph] Compre QA triggering follow-up")
        fu_res = await llm.ainvoke([
            SystemMessage(content=COMPRE_FOLLOWUP_SYSTEM),
            HumanMessage(content=f"【提问】{current_q}\n【回答】{query}"),
        ])
        followup_q = fu_res.content.strip()
        return {
            "question_compre": followup_q,
            "reply": followup_q,
            "follow_up_count": follow_up_count + 1,
            "is_answering": True,
            "raw_scores": archive,
        }
    else:
        logger.info("[LangGraph] Compre QA finished, advancing to report")
        return {
            "current_phase": "report",
            "is_answering": False,
            "raw_scores": archive,
        }


async def node_report(state: InterviewState) -> dict:
    logger.info("[LangGraph] Generating final interview report")
    archive = state.get("raw_scores", "")

    llm = _get_llm(temperature=0.2)
    report_res = await llm.ainvoke([
        SystemMessage(content=FINAL_REPORT_SYSTEM),
        HumanMessage(content=f"这是该考生本场面试的完整文字记录：\n{archive}\n请严格按 JSON 格式输出最终评分报告。"),
    ])

    raw_text = report_res.content.strip()
    raw_text = re.sub(r"^```json\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text).strip()

    formatted_report = "面试评分报告生成中..."
    closing_speech = "同学，今天的面试到这里就圆满结束了。感谢你的参与，结果请等待官方通知。"

    try:
        data = json.loads(raw_text)
        total = data.get("total_score", 85)
        scores = data.get("scores", {})
        weakness = data.get("core_weakness", "专业深度需继续拓展")
        tips = data.get("improvement_tips", "建议巩固底层原理并增加实际项目实践")
        closing = data.get("interviewer_closing", closing_speech)

        formatted_report = (
            "━━━━━━━━━━━━━━━\n"
            "🎓 考研复试模拟成绩单\n"
            "━━━━━━━━━━━━━━━\n"
            f"🏆 总评得分：{total}分\n\n"
            "📊 分项维度：\n"
            f"• 专业知识：{scores.get('profession_know', 80)}分\n"
            f"• 逻辑表达：{scores.get('exp_logic', 85)}分\n"
            f"• 综合素质：{scores.get('comper_quality', 85)}分\n\n"
            f"❌ 核心不足：\n{weakness}\n\n"
            f"💡 改进建议：\n{tips}\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"{closing}"
        )
        closing_speech = f"面试结束。总评得分 {total} 分。{closing}"
    except Exception as e:
        logger.warning("[LangGraph] JSON report parsing failed, falling back to text: %s", e)
        formatted_report = f"面试结束。\n{raw_text}"
        closing_speech = "面试已结束，非常感谢同学的配合，稍后可在后台查看详细报告。"

    return {
        "reply": closing_speech,
        "final_report": formatted_report,
        "current_phase": "completed",
    }


# ==========================================
# Router
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
# Graph Builder
# ==========================================
def create_interview_graph():
    builder = StateGraph(InterviewState)

    builder.add_node("self_intro", node_self_intro)
    builder.add_node("pro_qa", node_pro_qa)
    builder.add_node("compre_qa", node_compre_qa)
    builder.add_node("report", node_report)

    builder.set_conditional_entry_point(
        route_phase,
        {
            "self_intro": "self_intro",
            "pro_qa": "pro_qa",
            "compre_qa": "compre_qa",
            "report": "report",
            "end": END,
        },
    )

    builder.add_edge("self_intro", END)
    builder.add_edge("pro_qa", END)
    builder.add_edge("compre_qa", END)
    builder.add_edge("report", END)

    memory = MemorySaver()
    return builder.compile(checkpointer=memory)


interview_graph = create_interview_graph()

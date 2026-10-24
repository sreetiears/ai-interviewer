"""End-to-end state machine flow with a deterministic fake LLM.

The graph is compiled over a MemorySaver checkpointer, so these tests drive a
whole interview turn by turn and assert the phase transitions and that the final
score report is produced on the last answer.
"""
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from unittest.mock import AsyncMock, MagicMock, patch

from interview_graph import (
    COMPRE_JUDGE_SYSTEM,
    COMPRE_QUESTION_SYSTEM,
    FINAL_REPORT_SYSTEM,
    INTRO_JUDGE_SYSTEM,
    PRO_JUDGE_SYSTEM,
    PRO_QUESTION_SYSTEM,
    TRANSITION_TO_COMPRE_SYSTEM,
    create_interview_graph,
)

REPORT_JSON = (
    '{"total_score": 87, "scores": {"profession_know": 85, "exp_logic": 90, "comper_quality": 86}, '
    '"core_weakness": "底层原理需加深", "improvement_tips": "多读经典教材", '
    '"interviewer_closing": "感谢你的参与，请等待结果。"}'
)


def _fake_llm_response(messages) -> AIMessage:
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


def _mock_llm() -> MagicMock:
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=lambda messages: _fake_llm_response(messages))
    return mock


@pytest.mark.asyncio
async def test_full_interview_flow_produces_report_on_last_answer():
    graph = create_interview_graph()
    config = {"configurable": {"thread_id": "flow-test-1"}}

    with patch("interview_graph._get_llm", return_value=_mock_llm()):
        state = await graph.ainvoke(
            {
                "user_query": (
                    "各位老师好，我本科毕业于某某大学软件工程专业，在校期间参与过分布式微服务项目开发，"
                    "也参加过蓝桥杯竞赛并获奖，报考贵校是希望深入研究系统架构与分布式系统。"
                )
            },
            config,
        )
        assert state["current_phase"] == "pro_qa"
        assert state["is_answering"] is False

        state = await graph.ainvoke({"user_query": "好的老师，我准备好了。"}, config)
        assert state["current_phase"] == "pro_qa"
        assert state["question_pro"] == "请解释进程与线程的区别。"
        assert state["is_answering"] is True

        state = await graph.ainvoke({"user_query": "进程是资源分配单位，线程是调度单位。"}, config)
        assert state["current_phase"] == "compre_qa"
        assert state["is_answering"] is False

        state = await graph.ainvoke({"user_query": "好的。"}, config)
        assert state["question_compre"] == "请讲一次你在团队合作中遇到的冲突。"
        assert state["is_answering"] is True

        # The final answer must both close the interview and deliver the report.
        state = await graph.ainvoke(
            {"user_query": "当时我和队友在方案上意见不同，我主动组织了一次讨论并达成了折中方案。"},
            config,
        )

    assert state["current_phase"] == "completed"
    assert state["report_data"]["total_score"] == 87
    assert state["report_data"]["scores"]["exp_logic"] == 90
    assert "总评得分：87分" in state["final_report"]
    assert "【综合提问】" in state["raw_scores"]
    assert "面试结束" in state["reply"]


@pytest.mark.asyncio
async def test_real_mode_asks_three_questions_per_phase():
    graph = create_interview_graph()
    config = {"configurable": {"thread_id": "flow-test-real"}}

    with patch("interview_graph._get_llm", return_value=_mock_llm()):
        state = await graph.ainvoke(
            {
                "user_query": (
                    "各位老师好，我本科毕业于某某大学软件工程专业，在校期间参与过分布式微服务项目开发，"
                    "也参加过蓝桥杯竞赛并获奖，报考贵校是希望深入研究系统架构与分布式系统。"
                ),
                "interview_mode": "real",
            },
            config,
        )
        assert state["current_phase"] == "pro_qa"

        state = await graph.ainvoke({"user_query": "好的老师。"}, config)  # asks question 1
        assert state["questions_asked"] == 1
        for _ in range(3):  # answer three main questions in a row
            state = await graph.ainvoke({"user_query": "我的回答。"}, config)
        assert state["current_phase"] == "compre_qa"
        assert state["raw_scores"].count("【专业提问】") == 3

    with patch("interview_graph._get_llm", return_value=_mock_llm()):
        state = await graph.ainvoke({"user_query": "好的老师。"}, config)  # asks compre q1
        assert state["questions_asked"] == 1
        for _ in range(3):
            state = await graph.ainvoke({"user_query": "我的回答。"}, config)

    assert state["current_phase"] == "completed"
    assert state["raw_scores"].count("【综合提问】") == 3
    assert state["report_data"]["total_score"] == 87

    # A turn that arrives after the interview ended must not replay the closing line.
    with patch("interview_graph._get_llm", return_value=_mock_llm()):
        state = await graph.ainvoke({"user_query": "老师，我还能再说两句吗？"}, config)

    assert state["current_phase"] == "completed"
    assert "面试已经结束了" in state["reply"]


@pytest.mark.asyncio
async def test_graph_streams_reply_tokens_as_custom_deltas():
    """Reply tokens must be emitted through the custom stream so TTS can start early."""
    fake_llm = FakeListChatModel(responses=["不合格", "请补充本科经历与报考动机。"])
    graph = create_interview_graph()
    config = {"configurable": {"thread_id": "flow-test-stream"}}

    deltas: list[str] = []
    final_state: dict = {}

    with patch("interview_graph._get_llm", return_value=fake_llm):
        async for mode, chunk in graph.astream(
            {"user_query": "我叫张三。"},
            config,
            stream_mode=["custom", "values"],
        ):
            if mode == "custom" and chunk.get("type") == "delta":
                deltas.append(chunk["text"])
            elif mode == "values":
                final_state = chunk

    assert "".join(deltas) == "请补充本科经历与报考动机。"
    assert final_state["reply"] == "请补充本科经历与报考动机。"
    assert final_state["current_phase"] == "self_intro"

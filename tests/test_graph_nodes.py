import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage
from interview_graph import (
    _build_report_payload,
    _needs_followup,
    node_self_intro,
    node_pro_qa,
    node_compre_qa,
    node_report,
)


@pytest.mark.asyncio
async def test_self_intro_unqualified_flow():
    mock_llm = MagicMock()
    # Judge returns 不合格, tutor returns guidance
    mock_llm.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="不合格"),
        AIMessage(content="请补充本科经历与报考动机。"),
    ])

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {"user_query": "我叫张三，我想考贵校。"}
        result = await node_self_intro(state)

        assert result["current_phase"] == "self_intro"
        assert result["is_answering"] is True
        assert "请补充" in result["reply"]


@pytest.mark.asyncio
async def test_self_intro_qualified_flow():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="合格"))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "user_query": "各位老师好，我本科毕业于某某大学软件工程专业，在校期间参与过分布式微服务项目开发，报考贵校希望能深入研究系统架构。"
        }
        result = await node_self_intro(state)

        assert result["current_phase"] == "pro_qa"
        assert result["is_answering"] is False
        assert "专业知识" in result["reply"]


@pytest.mark.asyncio
async def test_pro_qa_initial_question():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="请简要阐述进程与线程的区别？"))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {"current_phase": "pro_qa", "is_answering": False, "question_pro": ""}
        result = await node_pro_qa(state)

        assert result["question_pro"] == "请简要阐述进程与线程的区别？"
        assert result["is_answering"] is True
        assert result["follow_up_count"] == 0


@pytest.mark.asyncio
async def test_pro_qa_followup():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="需要"),
        AIMessage(content="能否从内存空间和上下文切换开销进一步说明？"),
    ])

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "current_phase": "pro_qa",
            "is_answering": True,
            "question_pro": "请简要阐述进程与线程的区别？",
            "user_query": "进程是资源分配单位，线程是调度单位。",
            "follow_up_count": 0,
        }
        result = await node_pro_qa(state)

        assert result["follow_up_count"] == 1
        assert "内存空间" in result["reply"]


@pytest.mark.asyncio
async def test_final_report_generation():
    mock_llm = MagicMock()
    mock_json = (
        '{"total_score": 90, "scores": {"profession_know": 88, "exp_logic": 92, "comper_quality": 90}, '
        '"core_weakness": "底层实现细节需进一步熟练", "improvement_tips": "多阅读源码", "interviewer_closing": "辛苦了！"}'
    )
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=mock_json))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {"raw_scores": "【面试记录】"}
        result = await node_report(state)

        assert result["current_phase"] == "completed"
        assert "90" in result["final_report"]
        assert "总评得分" in result["final_report"]
        assert result["report_data"]["total_score"] == 90
        assert result["report_data"]["scores"]["profession_know"] == 88
        assert result["report_data"]["core_weakness"] == "底层实现细节需进一步熟练"


@pytest.mark.asyncio
async def test_compre_final_answer_generates_report_in_same_turn():
    """The report must be produced on the last answer, not one silent turn later."""
    mock_llm = MagicMock()
    mock_json = (
        '{"total_score": 81, "scores": {"profession_know": 78, "exp_logic": 84, "comper_quality": 82}, '
        '"core_weakness": "例子偏少", "improvement_tips": "补充细节", "interviewer_closing": "辛苦了！"}'
    )
    mock_llm.ainvoke = AsyncMock(side_effect=[AIMessage(content="不需要"), AIMessage(content=mock_json)])

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "current_phase": "compre_qa",
            "is_answering": True,
            "question_compre": "谈谈你遇到的团队冲突。",
            "user_query": "我在项目中负责协调，最后大家达成一致。",
            "follow_up_count": 0,
            "questions_asked": 1,
            "raw_scores": "【前期记录】",
        }
        result = await node_compre_qa(state)

        assert result["current_phase"] == "completed"
        assert result["report_data"]["total_score"] == 81
        assert "总评得分" in result["final_report"]
        assert "【前期记录】" in result["raw_scores"]
        assert "团队冲突" in result["raw_scores"]


def test_needs_followup_normalizes_judge_output():
    assert _needs_followup("需要") is True
    assert _needs_followup(" 需要。\n") is True
    assert _needs_followup("不需要") is False
    assert _needs_followup("不需要，回答很完整") is False
    assert _needs_followup("") is False


def test_build_report_payload_clamps_and_defaults_scores():
    payload = _build_report_payload(
        {
            "total_score": 86,
            "scores": {"profession_know": 150, "exp_logic": "88", "comper_quality": None},
        }
    )

    assert payload["total_score"] == 86
    assert payload["scores"]["profession_know"] == 100  # clamped to 100
    assert payload["scores"]["exp_logic"] == 88  # numeric string accepted
    assert payload["scores"]["comper_quality"] == 85  # None -> default
    assert payload["core_weakness"]
    assert payload["improvement_tips"]
    assert payload["closing"]


def test_build_report_payload_rejects_missing_total_score():
    with pytest.raises(ValueError):
        _build_report_payload({"scores": {"profession_know": 80}})


@pytest.mark.asyncio
async def test_report_parse_failure_does_not_fabricate_scores():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="{}"))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_report({"raw_scores": "【记录】"})

    assert result["report_data"] == {}
    assert result["current_phase"] == "completed"
    assert "总评得分" not in result["final_report"]


@pytest.mark.asyncio
async def test_self_intro_retry_cap_forces_advance():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="不合格"))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "user_query": "我叫张三。",
            "intro_attempts": 1,  # quick mode allows exactly one retry
        }
        result = await node_self_intro(state)

    assert result["current_phase"] == "pro_qa"
    assert result["is_answering"] is False
    assert result["questions_asked"] == 0
    assert "专业知识问答" in result["reply"]


@pytest.mark.asyncio
async def test_real_mode_asks_multiple_pro_questions():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        side_effect=[AIMessage(content="不需要"), AIMessage(content="第二道专业课问题。")]
    )

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "current_phase": "pro_qa",
            "interview_mode": "real",
            "is_answering": True,
            "question_pro": "第一道专业课问题。",
            "user_query": "我的回答。",
            "follow_up_count": 0,
            "questions_asked": 1,
            "raw_scores": "",
        }
        result = await node_pro_qa(state)

    assert result.get("current_phase", "pro_qa") == "pro_qa"  # still in the professional phase
    assert result["question_pro"] == "第二道专业课问题。"
    assert result["questions_asked"] == 2
    assert result["is_answering"] is True
    assert "第一道专业课问题" in result["raw_scores"]


@pytest.mark.asyncio
async def test_quick_mode_advances_after_first_answer():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        side_effect=[AIMessage(content="不需要"), AIMessage(content="好的，专业课结束。")]
    )

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "current_phase": "pro_qa",
            "interview_mode": "quick",
            "is_answering": True,
            "question_pro": "第一道专业课问题。",
            "user_query": "我的回答。",
            "follow_up_count": 0,
            "questions_asked": 1,
            "raw_scores": "",
        }
        result = await node_pro_qa(state)

    assert result["current_phase"] == "compre_qa"
    assert result["questions_asked"] == 0
    assert result["is_answering"] is False

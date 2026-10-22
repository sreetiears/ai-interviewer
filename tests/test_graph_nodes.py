import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage
from interview_graph import node_self_intro, node_pro_qa, node_compre_qa, node_report


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

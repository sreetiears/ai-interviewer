import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage
from interview_graph import (
    _build_report_payload,
    _needs_followup,
    _parse_judge_payload,
    _question_prompt,
    node_self_intro,
    node_pro_qa,
    node_compre_qa,
    node_report,
)
from learner_store import PRO_TOPICS


@pytest.mark.asyncio
async def test_self_intro_unqualified_flow():
    mock_llm = MagicMock()
    # 评判返回“不合格”，辅导返回引导语
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
    """成绩单必须在最后一次回答时生成，而不是再空等一轮。"""
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


def test_parse_judge_payload_accepts_json_and_legacy_text():
    parsed = _parse_judge_payload(
        '{"followup":"需要","topic":"os.memory","verdict":"weak"}',
        allowed_topics=frozenset(PRO_TOPICS),
        default_topic="se.design",
    )
    assert parsed == {"followup": "需要", "topic": "os.memory", "verdict": "weak"}

    legacy = _parse_judge_payload(
        "不需要",
        allowed_topics=frozenset(PRO_TOPICS),
        default_topic="se.design",
    )
    assert legacy["followup"] == "不需要"
    assert legacy["topic"] == "se.design"
    assert legacy["verdict"] == "ok"


def test_question_prompt_includes_focus_and_snapshot():
    prompt = _question_prompt(
        "请出一道题",
        {
            "asked_questions": "- 旧题\n",
            "focus_topics": "os.memory,cn.tcp",
            "profile_snapshot": "历史薄弱点：操作系统·内存",
        },
    )
    assert "旧题" in prompt
    assert "历史薄弱点" in prompt
    assert "操作系统·内存" in prompt
    assert "计算机网络·TCP" in prompt


def test_build_report_payload_clamps_scores_and_keeps_missing_as_none():
    payload = _build_report_payload(
        {
            "total_score": 86,
            "scores": {"profession_know": 150, "exp_logic": "88", "comper_quality": None},
            "weak_topics": ["os.memory", "bogus"],
        }
    )

    assert payload["total_score"] == 86  # 只有一项缺测：保留有效总分，不用平均值替换
    assert payload["scores"]["profession_know"] == 100  # 截断到 100
    assert payload["scores"]["exp_logic"] == 88  # 接受数字字符串
    assert payload["scores"]["comper_quality"] is None  # None 是缺测，不是默认分
    assert payload["incomplete"] is True
    assert payload["core_weakness"]
    assert payload["improvement_tips"]
    assert payload["closing"]
    assert payload["weak_topics"] == ["os.memory"]


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (84, 84),  # 与三项平均 82 相差正好 2 分：保留
        (80, 80),  # 低 2 分：保留
        (85, 82),  # 高 3 分：改为平均
        (79, 82),  # 低 3 分：同样改为平均
    ],
)
def test_build_report_payload_pulls_total_to_mean_only_beyond_two_points(total, expected):
    payload = _build_report_payload(
        {
            "total_score": total,
            "scores": {"profession_know": 80, "exp_logic": 82, "comper_quality": 84},
        }
    )

    assert payload["total_score"] == expected
    assert payload["scores"] == {"profession_know": 80, "exp_logic": 82, "comper_quality": 84}
    assert payload["incomplete"] is False


def test_build_report_payload_rounds_the_mean_when_it_replaces_total():
    # 三项平均 80.67，四舍五入为 81；总分 83 与平均相差 2.33 分，超过 2 分
    payload = _build_report_payload(
        {
            "total_score": 83,
            "scores": {"profession_know": 80, "exp_logic": 81, "comper_quality": 81},
        }
    )

    assert payload["total_score"] == 81


def test_build_report_payload_keeps_valid_total_when_a_subscore_is_missing():
    payload = _build_report_payload(
        {"total_score": 90, "scores": {"profession_know": 60, "comper_quality": 100}}
    )

    # 缺测不参与平均：若把缺测当 0，平均只有 53，总分会被错误地拉低。
    assert payload["total_score"] == 90
    assert payload["scores"] == {"profession_know": 60, "exp_logic": None, "comper_quality": 100}
    assert payload["incomplete"] is True


def test_build_report_payload_without_scores_marks_all_three_missing():
    payload = _build_report_payload({"total_score": 78})

    assert payload["total_score"] == 78
    assert payload["scores"] == {"profession_know": None, "exp_logic": None, "comper_quality": None}
    assert payload["incomplete"] is True


def test_build_report_payload_keeps_real_zero_as_a_score_not_missing():
    payload = _build_report_payload(
        {"total_score": 0, "scores": {"profession_know": 0, "exp_logic": 0, "comper_quality": 0}}
    )

    assert payload["total_score"] == 0
    assert payload["scores"] == {"profession_know": 0, "exp_logic": 0, "comper_quality": 0}
    assert payload["incomplete"] is False


@pytest.mark.parametrize("bad", ["高", "", "nan", True, [], {}, float("inf")])
def test_build_report_payload_treats_non_numeric_subscores_as_missing(bad):
    payload = _build_report_payload(
        {"total_score": 80, "scores": {"profession_know": bad, "exp_logic": 75, "comper_quality": 80}}
    )

    assert payload["scores"]["profession_know"] is None
    assert payload["total_score"] == 80
    assert payload["incomplete"] is True


@pytest.mark.parametrize("bad_total", [None, "高", "", True, float("inf"), float("nan")])
def test_build_report_payload_rejects_unusable_total_even_when_subscores_exist(bad_total):
    # 总分缺失时不由三项分数推算，走解析失败的回退。
    with pytest.raises(ValueError):
        _build_report_payload(
            {
                "total_score": bad_total,
                "scores": {"profession_know": 80, "exp_logic": 82, "comper_quality": 84},
            }
        )


def test_report_text_names_expression_logic_and_keeps_the_note():
    from interview_graph import _format_report_text

    payload = _build_report_payload(
        {
            "total_score": 80,
            "scores": {"profession_know": 70, "exp_logic": 75, "comper_quality": 80},
            "exp_logic_note": "停顿很少。",
            "core_weakness": "例子少",
            "improvement_tips": "补充例子",
            "interviewer_closing": "辛苦了",
        }
    )
    payload["delivery"] = {
        "measured_turns": 1,
        "missing_turns": 1,
        "display": {
            "duration": "3.4 秒",
            "speech_rate": "2.0 字/秒",
            "pause_ratio": "11.8%",
            "mean_pause": "0.4 秒",
            "pitch_std": "0.0 赫兹",
            "intensity": "77.0 分贝",
            "filler_ratio": "16.7%",
        },
    }

    assert payload["exp_logic_note"] == "停顿很少。"
    text = _format_report_text(payload)
    assert "表达逻辑：75分" in text
    assert "逻辑表达" not in text
    assert "表达说明：停顿很少。" in text
    assert "语速：2.0 字/秒" in text
    assert "缺测：1 轮" in text


def test_report_text_shows_missing_subscore_as_missing_not_zero():
    from interview_graph import _format_report_text

    payload = _build_report_payload(
        {
            "total_score": 80,
            "scores": {"profession_know": 78, "exp_logic": None, "comper_quality": 82},
            "core_weakness": "例子少",
            "improvement_tips": "补充例子",
            "interviewer_closing": "辛苦了",
        }
    )

    text = _format_report_text(payload)
    assert "• 专业知识：78分" in text
    assert "• 表达逻辑：缺测" in text
    assert "• 综合素质：82分" in text
    assert "表达逻辑：0分" not in text
    assert "本场成绩单不完整" in text


def test_report_text_of_complete_report_has_no_missing_mark():
    from interview_graph import _format_report_text

    payload = _build_report_payload(
        {
            "total_score": 80,
            "scores": {"profession_know": 78, "exp_logic": 80, "comper_quality": 82},
        }
    )

    text = _format_report_text(payload)
    assert "缺测" not in text
    assert "不完整" not in text


@pytest.mark.asyncio
async def test_report_uses_measured_delivery_instead_of_model_numbers(monkeypatch):
    monkeypatch.setenv("DELIVERY_METRICS", "1")
    mock_llm = MagicMock()
    mock_json = (
        '{"total_score": 80, "scores": {"profession_know": 70, "exp_logic": 75, "comper_quality": 80}, '
        '"core_weakness": "例子少", "improvement_tips": "补充例子", "interviewer_closing": "辛苦了", '
        '"exp_logic_note": "语速稳定。"}'
    )
    seen = {}

    async def _invoke(messages):
        seen["messages"] = messages
        return AIMessage(content=mock_json)

    mock_llm.ainvoke = _invoke
    turn = {
        "missing": False,
        "duration_seconds": 3.4,
        "voiced_seconds": 3.0,
        "pause_seconds": 0.4,
        "pause_count": 1,
        "speech_rate_chars_per_second": 2.0,
        "pause_ratio": 0.4 / 3.4,
        "mean_pause_seconds": 0.4,
        "pitch_std_hz": 0.0,
        "intensity_db": 77.0,
        "filler_ratio": 1 / 6,
        "transcript_chars": 6,
        "filler_chars": 1,
    }

    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_report({"raw_scores": "【记录】", "delivery_turns": [turn]})

    human = seen["messages"][1].content
    assert "语速 2.0 字/秒" in human
    assert result["report_data"]["delivery"]["speech_rate_chars_per_second"] == 2.0
    assert result["report_data"]["exp_logic_note"] == "语速稳定。"
    assert "表达逻辑" in result["final_report"]


@pytest.mark.asyncio
async def test_report_ignores_delivery_when_the_switch_is_off(monkeypatch):
    monkeypatch.setenv("DELIVERY_METRICS", "0")
    mock_llm = MagicMock()
    mock_json = (
        '{"total_score": 80, "scores": {"profession_know": 70, "exp_logic": 75, "comper_quality": 80}, '
        '"core_weakness": "例子少", "improvement_tips": "补充例子", "interviewer_closing": "辛苦了"}'
    )
    seen = {}

    async def _invoke(messages):
        seen["messages"] = messages
        return AIMessage(content=mock_json)

    mock_llm.ainvoke = _invoke

    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_report(
            {
                "raw_scores": "【记录】",
                "delivery_turns": [{"missing": False, "speech_rate_chars_per_second": 2.0}],
            }
        )

    assert seen["messages"][1].content == (
        "这是该考生本场面试的完整文字记录：\n【记录】\n请严格按 JSON 格式输出最终评分报告。"
    )
    assert "delivery" not in result["report_data"]
    assert "字/秒" not in result["final_report"]


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
async def test_report_parse_failure_persists_total_as_missing_not_zero():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="{}"))
    store = MagicMock()

    with patch("interview_graph._get_llm", return_value=mock_llm), patch(
        "interview_graph.get_store", return_value=store
    ):
        await node_report(
            {"raw_scores": "【记录】", "learner_id": "learner-0123456789ab", "session_id": "s-1"}
        )

    saved = store.record_session.call_args.kwargs["report"]
    assert saved["total_score"] is None


@pytest.mark.asyncio
async def test_final_report_pulls_conflicting_total_to_subscore_mean():
    mock_llm = MagicMock()
    mock_json = (
        '{"total_score": 95, "scores": {"profession_know": 80, "exp_logic": 80, "comper_quality": 80}, '
        '"core_weakness": "例子少", "improvement_tips": "补充例子", "interviewer_closing": "辛苦了"}'
    )
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=mock_json))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_report({"raw_scores": "【记录】"})

    assert result["report_data"]["total_score"] == 80
    assert "总评得分：80分" in result["final_report"]
    assert "总评得分 80 分" in result["reply"]


@pytest.mark.asyncio
async def test_final_report_marks_missing_subscore_and_keeps_valid_total():
    mock_llm = MagicMock()
    mock_json = (
        '{"total_score": 90, "scores": {"profession_know": 88, "exp_logic": null, "comper_quality": 92}, '
        '"core_weakness": "例子少", "improvement_tips": "补充例子", "interviewer_closing": "辛苦了"}'
    )
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=mock_json))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_report({"raw_scores": "【记录】"})

    report = result["report_data"]
    assert report["total_score"] == 90
    assert report["scores"]["exp_logic"] is None
    assert report["incomplete"] is True
    assert "表达逻辑：缺测" in result["final_report"]
    assert "总评得分：90分" in result["final_report"]


@pytest.mark.asyncio
async def test_self_intro_retry_cap_forces_advance():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="不合格"))

    with patch("interview_graph._get_llm", return_value=mock_llm):
        state = {
            "user_query": "我叫张三。",
            "intro_attempts": 1,  # quick 模式只允许重试一次
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

    assert result.get("current_phase", "pro_qa") == "pro_qa"  # 仍处在专业课阶段
    assert result["question_pro"] == "第二道专业课问题。"
    assert result["questions_asked"] == 2
    assert result["is_answering"] is True
    assert "第一道专业课问题" in result["raw_scores"]


@pytest.mark.asyncio
async def test_next_question_prompt_includes_questions_already_asked():
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
            "asked_questions": "- 第一道专业课问题。\n",
            "raw_scores": "",
        }
        result = await node_pro_qa(state)

    question_prompt = mock_llm.ainvoke.await_args_list[1].args[0][1].content
    assert "第一道专业课问题" in question_prompt
    assert "第二道专业课问题" in result["asked_questions"]
    assert "第一道专业课问题" in result["asked_questions"]
    assert "turn_tags" in result
    assert "os." in result["turn_tags"] or "se." in result["turn_tags"] or "ds." in result["turn_tags"] or "cn." in result["turn_tags"]


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

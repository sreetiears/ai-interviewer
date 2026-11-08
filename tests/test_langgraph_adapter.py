"""LangGraph 与 LiveKit 适配器的行为测试。

这些测试用假图驱动真实的 ``LLMStream`` 管线（后台任务、异步分块迭代），
从而在不访问网络的情况下，验证 Web 前端使用的界面事件协议和 token 流。
"""
import pytest
from livekit.agents import llm

from langgraph_adapter import LangGraphLLM, user_facing_error_message


class FakeGraph:
    """编译后 LangGraph 的替身，返回预设状态。"""

    def __init__(self, result: dict, custom_chunks: list[dict] | None = None) -> None:
        self.result = result
        self.custom_chunks = custom_chunks or []
        self.calls: list[dict] = []

    async def astream(self, input_state, config=None, stream_mode=None):
        self.calls.append({"input": input_state, "config": config, "stream_mode": stream_mode})
        for chunk in self.custom_chunks:
            yield ("custom", chunk)
        yield ("values", dict(self.result))


def _user_chat_ctx(text: str) -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content=text)
    return ctx


async def _read_reply(stream) -> str:
    parts: list[str] = []
    async for chunk in stream:
        content = getattr(getattr(chunk, "delta", None), "content", None)
        if content:
            parts.append(content)
    return "".join(parts)


@pytest.mark.asyncio
async def test_stream_emits_thinking_and_phase_events():
    events: list[dict] = []
    adapter = LangGraphLLM(thread_id="t-1", on_event=events.append)
    adapter.graph = FakeGraph({"reply": "请谈谈进程与线程的区别。", "current_phase": "pro_qa"})

    reply = await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("好的老师")))

    assert "进程与线程" in reply
    assert {"type": "status", "state": "thinking"} in events
    assert {"type": "phase", "phase": "pro_qa"} in events
    assert {"type": "status", "state": "speaking"} in events
    assert adapter.graph.calls[0]["input"]["user_query"] == "好的老师"
    assert adapter.graph.calls[0]["input"]["interview_mode"] == "quick"
    assert adapter.graph.calls[0]["config"] == adapter.config
    assert adapter.graph.calls[0]["stream_mode"] == ["custom", "values"]


@pytest.mark.asyncio
async def test_stream_forwards_custom_token_deltas_without_duplicating_reply():
    events: list[dict] = []
    adapter = LangGraphLLM(thread_id="t-1b", on_event=events.append)
    adapter.graph = FakeGraph(
        {"reply": "你好，同学。", "current_phase": "self_intro"},
        custom_chunks=[
            {"type": "delta", "text": "你好"},
            {"type": "delta", "text": "，同学。"},
        ],
    )

    reply = await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("老师好")))

    # token 只通过自定义流到达一次；最终回复不能再追加第二遍。
    assert reply == "你好，同学。"


@pytest.mark.asyncio
async def test_stream_emits_report_event_on_completion():
    report = {
        "total_score": 88,
        "scores": {"profession_know": 86, "exp_logic": 90, "comper_quality": 89},
        "core_weakness": "底层原理不熟",
        "improvement_tips": "多读源码",
        "closing": "感谢参加",
    }
    events: list[dict] = []
    adapter = LangGraphLLM(thread_id="t-2", on_event=events.append)
    adapter.graph = FakeGraph(
        {
            "reply": "面试结束。",
            "current_phase": "completed",
            "final_report": "成绩单正文",
            "report_data": report,
        }
    )

    await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("我答完了")))

    reports = [event for event in events if event.get("type") == "report"]
    assert len(reports) == 1
    assert reports[0]["report"] == report
    assert reports[0]["text"] == "成绩单正文"
    assert {"type": "phase", "phase": "completed"} in events


@pytest.mark.asyncio
async def test_interview_mode_is_forwarded_to_graph():
    adapter = LangGraphLLM(thread_id="t-mode", interview_mode="real")
    adapter.graph = FakeGraph({"reply": "请继续。", "current_phase": "pro_qa"})

    await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("好的")))

    assert adapter.interview_mode == "real"
    assert adapter.graph.calls[0]["input"]["interview_mode"] == "real"


@pytest.mark.asyncio
async def test_unknown_interview_mode_falls_back_to_default():
    adapter = LangGraphLLM(thread_id="t-mode-bad", interview_mode="not-a-mode")
    adapter.graph = FakeGraph({"reply": "请继续。", "current_phase": "pro_qa"})

    await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("好的")))

    assert adapter.interview_mode == "quick"


@pytest.mark.asyncio
async def test_stream_without_user_message_skips_graph():
    """只有助手消息的上下文不能被当成考生回答再放一遍。"""
    adapter = LangGraphLLM(thread_id="t-3")
    adapter.graph = FakeGraph({"reply": "不该被执行"})
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="assistant", content="上一句考官提问")

    reply = await _read_reply(adapter.chat(chat_ctx=ctx))

    assert reply == ""
    assert adapter.graph.calls == []


@pytest.mark.asyncio
async def test_report_and_phase_are_emitted_once_per_session():
    """检查点状态会一直保留 report_data；界面不能收到两次。"""
    report = {
        "total_score": 88,
        "scores": {"profession_know": 86, "exp_logic": 90, "comper_quality": 89},
        "core_weakness": "底层原理不熟",
        "improvement_tips": "多读源码",
        "closing": "感谢参加",
    }
    events: list[dict] = []
    adapter = LangGraphLLM(thread_id="t-5", on_event=events.append)
    adapter.graph = FakeGraph(
        {"reply": "面试结束。", "current_phase": "completed", "report_data": report}
    )

    await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("我答完了")))
    await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("我还能再说两句吗")))

    assert len([e for e in events if e.get("type") == "report"]) == 1
    assert len([e for e in events if e.get("type") == "phase"]) == 1


@pytest.mark.asyncio
async def test_stream_falls_back_to_raw_report_text():
    """JSON 解析失败时仍有 final_report；界面应该收到它。"""
    events: list[dict] = []
    adapter = LangGraphLLM(thread_id="t-6", on_event=events.append)
    adapter.graph = FakeGraph(
        {
            "reply": "面试结束。",
            "current_phase": "completed",
            "final_report": "面试结束。\n{not-json}",
            "report_data": {},
        }
    )

    await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("我答完了")))

    reports = [e for e in events if e.get("type") == "report"]
    assert len(reports) == 1
    assert reports[0]["report"] == {}
    assert "not-json" in reports[0]["text"]


@pytest.mark.asyncio
async def test_broken_event_sink_does_not_break_reply():
    def broken_sink(event: dict) -> None:
        raise RuntimeError("ui sink down")

    adapter = LangGraphLLM(thread_id="t-4", on_event=broken_sink)
    adapter.graph = FakeGraph({"reply": "请继续。", "current_phase": "self_intro"})

    assert "请继续" in await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("你好")))


def test_user_facing_error_message_classifies_common_failures():
    assert "超时" in user_facing_error_message(TimeoutError("request timed out"))
    assert "鉴权" in user_facing_error_message(RuntimeError("401 Unauthorized: invalid api key"))
    assert "频繁" in user_facing_error_message(RuntimeError("429 Too Many Requests rate limit"))
    assert "请稍后再试" in user_facing_error_message(RuntimeError("boom"))


class BoomGraph:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def astream(self, input_state, config=None, stream_mode=None):
        raise self.exc
        yield  # pragma: no cover - make this an async generator


@pytest.mark.asyncio
async def test_stream_speaks_classified_error_message():
    events: list[dict] = []
    adapter = LangGraphLLM(thread_id="t-err", on_event=events.append)
    adapter.graph = BoomGraph(TimeoutError("deadline exceeded"))

    reply = await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("老师好")))

    assert "超时" in reply
    assert {"type": "status", "state": "listening"} in events

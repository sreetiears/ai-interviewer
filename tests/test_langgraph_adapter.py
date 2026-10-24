"""Behavioural tests for the LangGraph <-> LiveKit adapter.

These tests exercise the real ``LLMStream`` plumbing (background task, async
chunk iteration) with a fake graph, so the UI event contract and token streaming
used by the web frontend are verified without any network access.
"""
import pytest
from livekit.agents import llm

from langgraph_adapter import LangGraphLLM


class FakeGraph:
    """Stand-in for the compiled LangGraph returning a canned state."""

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

    # Tokens arrive once through the custom stream; the final reply must not be
    # appended a second time.
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
async def test_opening_greeting_bypasses_graph():
    adapter = LangGraphLLM(thread_id="t-3")
    adapter.graph = FakeGraph({"reply": "不该被执行"})

    reply = await _read_reply(
        adapter.chat(chat_ctx=_user_chat_ctx("请先向考生打招呼，并邀请对方开始自我介绍。"))
    )

    assert "自我介绍" in reply
    assert adapter.graph.calls == []


@pytest.mark.asyncio
async def test_broken_event_sink_does_not_break_reply():
    def broken_sink(event: dict) -> None:
        raise RuntimeError("ui sink down")

    adapter = LangGraphLLM(thread_id="t-4", on_event=broken_sink)
    adapter.graph = FakeGraph({"reply": "请继续。", "current_phase": "self_intro"})

    assert "请继续" in await _read_reply(adapter.chat(chat_ctx=_user_chat_ctx("你好")))

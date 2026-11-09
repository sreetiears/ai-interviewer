"""智能体会话接线与面试模式解析的单元测试。"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import (
    _build_session,
    _resolve_focus_topics,
    _resolve_interview_mode,
    _resolve_learner_id,
    _resolve_session_id,
    entrypoint,
)
from config import config
from langgraph_adapter import LangGraphLLM


@pytest.mark.asyncio
async def test_build_session_wires_langgraph_adapter_with_event_sink(monkeypatch):
    async def sink(event):  # pragma: no cover - 此处不会被调用
        return None

    monkeypatch.setattr("agent.silero.VAD.load", lambda **kwargs: object())

    agent_instance, session = _build_session(
        "candidate-1",
        on_event=sink,
        interview_mode="real",
        learner_id="learner-aaaabbbbcccc",
        focus_topics=["os.memory"],
        profile_snapshot="加强：操作系统·内存",
        session_id="sess-1",
    )

    assert isinstance(session.llm, LangGraphLLM)
    assert session.llm.thread_id == "candidate-1"
    assert session.llm._on_event is sink
    assert session.llm.interview_mode == "real"
    assert session.llm.learner_id == "learner-aaaabbbbcccc"
    assert session.llm.focus_topics == ["os.memory"]
    assert session.llm.profile_snapshot == "加强：操作系统·内存"
    assert session.llm.session_id == "sess-1"
    assert agent_instance is not None


@pytest.mark.asyncio
async def test_build_session_accepts_explicit_thread_id(monkeypatch):
    monkeypatch.setattr("agent.silero.VAD.load", lambda **kwargs: object())

    _, session = _build_session("candidate-1", thread_id="session-abc", interview_mode="quick")

    assert session.llm.thread_id == "session-abc"


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish_data(self, payload, *, reliable=True, topic="") -> None:
        self.published.append(
            {"payload": json.loads(payload.decode("utf-8")), "reliable": reliable, "topic": topic}
        )


class _FakeRoom:
    name = "ai-room"

    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


class _FakeSession:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.generated: list[dict] = []
        self.said: list[dict] = []

    async def start(self, **kwargs) -> None:
        self.started.append(kwargs)

    def generate_reply(self, **kwargs) -> None:
        self.generated.append(kwargs)

    def say(self, text, **kwargs) -> None:
        self.said.append({"text": text, **kwargs})


class _FakeJobContext:
    def __init__(self, metadata: str = "") -> None:
        self.room = _FakeRoom()
        self.metadata = metadata

    async def connect(self, **kwargs) -> None:
        return None

    async def wait_for_participant(self):
        return SimpleNamespace(identity="candidate-1", metadata=self.metadata)


@pytest.mark.asyncio
async def test_entrypoint_publishes_mode_and_initial_phase(monkeypatch):
    ctx = _FakeJobContext(
        metadata=json.dumps({"interview_mode": "real", "session_id": "session-1"})
    )
    session = _FakeSession()
    captured: dict = {}

    def fake_build_session(identity, on_event=None, interview_mode=None, thread_id=None, **kwargs):
        captured["identity"] = identity
        captured["on_event"] = on_event
        captured["interview_mode"] = interview_mode
        captured["thread_id"] = thread_id
        captured["kwargs"] = kwargs
        return SimpleNamespace(), session

    monkeypatch.setattr("agent._build_session", fake_build_session)
    monkeypatch.setattr(
        "agent.get_store",
        lambda: SimpleNamespace(
            build_snapshot=lambda learner_id, focus_topics=None: SimpleNamespace(
                focus_topics=list(focus_topics or []),
                prompt_text=lambda: "历史薄弱点：操作系统·内存",
            )
        ),
    )

    await entrypoint(ctx)

    assert captured["identity"] == "candidate-1"
    assert captured["on_event"] is not None
    assert captured["interview_mode"] == "real"
    assert captured["thread_id"] == "session-1"
    assert captured["kwargs"]["learner_id"] == "candidate-1"
    assert captured["kwargs"]["profile_snapshot"] == "历史薄弱点：操作系统·内存"
    published = ctx.room.local_participant.published
    assert published[0]["payload"] == {"type": "mode", "mode": "real"}
    assert published[1]["payload"] == {"type": "phase", "phase": "self_intro"}
    assert published[0]["topic"] == "interview"
    assert session.started and session.started[0]["room"] is ctx.room
    assert session.said
    assert session.said[0]["text"] == config.greeting_text
    assert session.said[0]["allow_interruptions"] is False
    assert not session.generated


@pytest.mark.asyncio
async def test_entrypoint_falls_back_to_room_and_identity_for_old_tokens(monkeypatch):
    ctx = _FakeJobContext(metadata=json.dumps({"interview_mode": "real"}))
    session = _FakeSession()
    captured: dict = {}

    def fake_build_session(identity, on_event=None, interview_mode=None, thread_id=None, **kwargs):
        captured["thread_id"] = thread_id
        return SimpleNamespace(), session

    monkeypatch.setattr("agent._build_session", fake_build_session)
    monkeypatch.setattr(
        "agent.get_store",
        lambda: SimpleNamespace(
            build_snapshot=lambda learner_id, focus_topics=None: SimpleNamespace(
                focus_topics=[],
                prompt_text=lambda: "",
            )
        ),
    )

    await entrypoint(ctx)

    assert captured["thread_id"] == "ai-room:candidate-1"


def test_resolve_session_id_from_participant_metadata():
    assert _resolve_session_id(SimpleNamespace(metadata='{"session_id": "abc-123"}')) == "abc-123"
    assert _resolve_session_id(SimpleNamespace(metadata='{"session_id": "bad id!"}')) == ""
    assert _resolve_session_id(SimpleNamespace(metadata='{"session_id": ""}')) == ""
    assert _resolve_session_id(SimpleNamespace(metadata="not json")) == ""
    assert _resolve_session_id(SimpleNamespace()) == ""


def test_resolve_interview_mode_from_participant_metadata():
    assert _resolve_interview_mode(SimpleNamespace(metadata='{"interview_mode": "real"}')) == "real"
    assert _resolve_interview_mode(SimpleNamespace(metadata='{"mode": "QUICK"}')) == "quick"
    assert _resolve_interview_mode(SimpleNamespace(metadata="not json")) == config.interview_mode
    assert _resolve_interview_mode(SimpleNamespace(metadata='{"interview_mode": "bogus"}')) == "quick"
    assert _resolve_interview_mode(SimpleNamespace()) == config.interview_mode


def test_resolve_learner_and_focus_from_metadata():
    participant = SimpleNamespace(
        metadata=json.dumps(
            {
                "learner_id": "learner-aaaabbbbcccc",
                "focus_topics": ["os.memory", "cn.tcp"],
            }
        )
    )
    assert _resolve_learner_id(participant, "fallback") == "learner-aaaabbbbcccc"
    assert _resolve_focus_topics(participant) == ["os.memory", "cn.tcp"]
    assert _resolve_learner_id(SimpleNamespace(metadata="{}"), "candidate-9") == "candidate-9"


def test_agent_has_no_dify_backend():
    """Dify 支持已移除；保持模块对外接口干净。"""
    import agent

    assert not hasattr(agent, "DifyLLM")
    assert not hasattr(agent, "DifyLLMStream")

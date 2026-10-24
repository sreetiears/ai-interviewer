"""Unit tests for the agent session wiring and interview-mode resolution."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import _build_session, _resolve_interview_mode, entrypoint
from config import config
from langgraph_adapter import LangGraphLLM


@pytest.mark.asyncio
async def test_build_session_wires_langgraph_adapter_with_event_sink(monkeypatch):
    async def sink(event):  # pragma: no cover - never called here
        return None

    monkeypatch.setattr("agent.silero.VAD.load", lambda **kwargs: object())

    agent_instance, session = _build_session("candidate-1", on_event=sink, interview_mode="real")

    assert isinstance(session.llm, LangGraphLLM)
    assert session.llm.thread_id == "candidate-1"
    assert session.llm._on_event is sink
    assert session.llm.interview_mode == "real"
    assert agent_instance is not None


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

    async def start(self, **kwargs) -> None:
        self.started.append(kwargs)

    def generate_reply(self, **kwargs) -> None:
        self.generated.append(kwargs)


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
    ctx = _FakeJobContext(metadata=json.dumps({"interview_mode": "real"}))
    session = _FakeSession()
    captured: dict = {}

    def fake_build_session(identity, on_event=None, interview_mode=None):
        captured["identity"] = identity
        captured["on_event"] = on_event
        captured["interview_mode"] = interview_mode
        return SimpleNamespace(), session

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("agent._build_session", fake_build_session)
    monkeypatch.setattr("agent.asyncio.sleep", no_sleep)

    await entrypoint(ctx)

    assert captured["identity"] == "candidate-1"
    assert captured["on_event"] is not None
    assert captured["interview_mode"] == "real"
    published = ctx.room.local_participant.published
    assert published[0]["payload"] == {"type": "mode", "mode": "real"}
    assert published[1]["payload"] == {"type": "phase", "phase": "self_intro"}
    assert published[0]["topic"] == "interview"
    assert session.started and session.started[0]["room"] is ctx.room
    assert session.generated
    assert session.generated[0]["user_input"] == config.opening_prompt
    assert session.generated[0]["input_modality"] == "text"


def test_resolve_interview_mode_from_participant_metadata():
    assert _resolve_interview_mode(SimpleNamespace(metadata='{"interview_mode": "real"}')) == "real"
    assert _resolve_interview_mode(SimpleNamespace(metadata='{"mode": "QUICK"}')) == "quick"
    assert _resolve_interview_mode(SimpleNamespace(metadata="not json")) == config.interview_mode
    assert _resolve_interview_mode(SimpleNamespace(metadata='{"interview_mode": "bogus"}')) == "quick"
    assert _resolve_interview_mode(SimpleNamespace()) == config.interview_mode


def test_agent_has_no_dify_backend():
    """Dify support was removed; keep the module surface clean."""
    import agent

    assert not hasattr(agent, "DifyLLM")
    assert not hasattr(agent, "DifyLLMStream")

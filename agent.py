import asyncio
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from dotenv import load_dotenv
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, voice
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins import openai, silero
from livekit.plugins.openai import tts as openai_tts_impl

# Load environment configuration
load_dotenv()
from config import config
from interview_graph import normalize_interview_mode
from langgraph_adapter import LangGraphLLM

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ai-interviewer")

# Data-channel topic the browser listens on for interview UI events.
INTERVIEW_EVENT_TOPIC = "interview"


def _safe_identity(identity: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", identity).strip("-")
    return normalized or f"candidate-{uuid.uuid4().hex[:6]}"


def _resolve_interview_mode(participant: Any) -> str:
    """Read the interview mode the browser selected from the participant metadata.

    The web server signs the access token with ``{"interview_mode": "quick"|"real"}``;
    LiveKit exposes that as ``participant.metadata``. Anything malformed falls back
    to the configured default so a bad client cannot break its own interview.
    """
    raw_metadata = getattr(participant, "metadata", "") or ""
    mode: Any = None
    try:
        payload = json.loads(raw_metadata)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        for key in ("interview_mode", "mode"):
            if payload.get(key):
                mode = payload[key]
                break
    resolved = normalize_interview_mode(str(mode) if mode else config.interview_mode)
    if mode and resolved != str(mode).strip().lower():
        logger.warning("Unknown interview mode %r, falling back to %r", mode, resolved)
    return resolved


class SiliconFlowTTS(openai_tts_impl.TTS):
    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> openai_tts_impl.AudioChunkedStream:
        cleaned_text = re.sub(r"\s+", " ", text).strip()
        return openai_tts_impl.AudioChunkedStream(
            tts=self,
            input_text=cleaned_text or text,
            conn_options=conn_options,
        )


def _build_session(
    participant_identity: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    interview_mode: str | None = None,
) -> tuple[voice.Agent, voice.AgentSession]:
    user_id = _safe_identity(participant_identity)
    resolved_mode = normalize_interview_mode(interview_mode or config.interview_mode)
    logger.info(
        "Initializing LangGraph interview session for thread: %s (mode: %s)",
        user_id,
        resolved_mode,
    )

    agent_instance = voice.Agent(instructions=config.agent_instructions)
    session = voice.AgentSession(
        # Every graph invocation advances interview state, so speculative preemptive
        # generation could double-consume a turn. Correctness beats a small latency win.
        preemptive_generation=False,
        vad=silero.VAD.load(
            min_speech_duration=0.6,
            min_silence_duration=1.5,
        ),
        stt=openai.STT(
            base_url=config.siliconflow_base_url,
            api_key=config.siliconflow_api_key or "sk-dummy",
            model=config.stt_model,
            language=config.stt_language,
        ),
        llm=LangGraphLLM(
            thread_id=user_id,
            on_event=on_event,
            interview_mode=resolved_mode,
        ),
        tts=SiliconFlowTTS(
            base_url=config.siliconflow_base_url,
            api_key=config.siliconflow_api_key or "sk-dummy",
            model=config.tts_model,
            voice=config.tts_voice,
            response_format=config.tts_response_format,
        ),
    )
    return agent_instance, session


async def entrypoint(ctx: JobContext) -> None:
    logger.info("Received interview job for room: %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("Connected to room, waiting for participant...")

    participant = await ctx.wait_for_participant()
    participant_identity = participant.identity or f"candidate-{uuid.uuid4().hex[:6]}"
    interview_mode = _resolve_interview_mode(participant)
    logger.info(
        "Participant joined: %s (interview mode: %s)", participant_identity, interview_mode
    )

    async def publish_event(event: dict[str, Any]) -> None:
        """Push UI events (phase / status / report) to the browser over the data channel."""
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
        try:
            await ctx.room.local_participant.publish_data(
                payload,
                reliable=True,
                topic=INTERVIEW_EVENT_TOPIC,
            )
        except Exception:
            logger.warning("Failed to publish interview event %r", event.get("type"), exc_info=True)

    agent_instance, session = _build_session(
        participant_identity,
        on_event=publish_event,
        interview_mode=interview_mode,
    )

    logger.info("Starting voice session")
    await session.start(agent=agent_instance, room=ctx.room)

    # Tell the browser which step and mode it starts in; later phases are pushed by
    # the LLM adapter as the interview progresses.
    await publish_event({"type": "mode", "mode": interview_mode})
    await publish_event({"type": "phase", "phase": "self_intro"})

    await asyncio.sleep(1)
    logger.info("Triggering opening message...")
    session.generate_reply(
        user_input=config.opening_prompt,
        allow_interruptions=False,
        input_modality="text",
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        logger.warning(
            "You started the agent with `dev`. This only registers a worker and waits for a job assignment. "
            "For local room testing, use `python agent.py connect --room ai-room` so the interviewer joins the room immediately."
        )
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

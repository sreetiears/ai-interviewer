import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
from dotenv import load_dotenv
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, vad, voice
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins import openai, silero
from livekit.plugins.openai import tts as openai_tts_impl

# 加载环境配置
load_dotenv()
from config import assert_runtime_ready, config, delivery_metrics_enabled, resolve_api_key
from interview_modes import normalize_mode as normalize_interview_mode
from langgraph_adapter import LangGraphLLM
from perception.delivery import TurnDelivery, measure_turn

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ai-interviewer")

# 浏览器监听的数据通道主题，用于接收面试界面事件。
INTERVIEW_EVENT_TOPIC = "interview"


def _safe_identity(identity: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", identity).strip("-")
    return normalized or f"candidate-{uuid.uuid4().hex[:6]}"


SESSION_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,128}$")


def _participant_metadata(participant: Any) -> dict[str, Any]:
    raw_metadata = getattr(participant, "metadata", "") or ""
    try:
        payload = json.loads(raw_metadata)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_session_id(participant: Any) -> str:
    """从参与者元数据中读取本次令牌对应的会话 id。

    Web 服务会在每张访问令牌里签入一个新的 id。把它用作 LangGraph 的
    ``thread_id``，可以让每场面试拥有独立检查点，这样同一房间里的两名
    考生——或持新令牌重新加入的同一名考生——都不会继承另一场的阶段、
    转写或最终成绩单。空字符串表示“旧令牌 / 没有 id”，调用方会回退到
    room:identity。
    """
    payload = _participant_metadata(participant)
    candidate = payload.get("session_id")
    if isinstance(candidate, str):
        candidate = candidate.strip()
        if SESSION_ID_RE.fullmatch(candidate):
            return candidate
    return ""


def _resolve_interview_mode(participant: Any) -> str:
    """从参与者元数据中读取浏览器选择的面试模式。

    Web 服务签发的访问令牌带有 ``{"interview_mode": "quick"|"real"}``；
    LiveKit 把它暴露为 ``participant.metadata``。格式异常时回退到配置的
    默认模式，避免错误的客户端弄坏自己的面试。
    """
    payload = _participant_metadata(participant)
    mode: Any = None
    if payload:
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


class _SpeechTap:
    """在考生这一轮结束前暂存波形，测量后即丢弃。"""

    def __init__(self) -> None:
        self._pieces: list[np.ndarray] = []
        self._sample_rate: int | None = None

    def add_frames(self, frames) -> None:
        if not delivery_metrics_enabled():
            return
        for frame in frames or []:
            samples, sample_rate = _frame_to_mono(frame)
            if samples.size == 0:
                continue
            if self._sample_rate is None:
                self._sample_rate = sample_rate
            elif sample_rate != self._sample_rate:
                samples = _resample(samples, sample_rate, self._sample_rate)
            self._pieces.append(samples)

    def take(self) -> tuple[np.ndarray, int] | None:
        pieces = self._pieces
        sample_rate = self._sample_rate
        self._pieces = []
        self._sample_rate = None
        if not pieces or sample_rate is None:
            return None
        return np.concatenate(pieces), sample_rate


class _TapVAD(vad.VAD):
    def __init__(self, inner: vad.VAD, tap: _SpeechTap) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner = inner
        self._tap = tap

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    def stream(self) -> "_TapStream":
        return _TapStream(self, self._inner.stream(), self._tap)


class _TapStream(vad.VADStream):
    def __init__(self, parent: _TapVAD, inner: vad.VADStream, tap: _SpeechTap) -> None:
        self._inner_stream = inner
        self._tap = tap
        super().__init__(parent)

    async def _main_task(self) -> None:
        async def _pump_in() -> None:
            async for item in self._input_ch:
                if isinstance(item, vad.VADStream._FlushSentinel):
                    self._inner_stream.flush()
                else:
                    self._inner_stream.push_frame(item)
            self._inner_stream.end_input()

        pump = asyncio.create_task(_pump_in())
        try:
            async for event in self._inner_stream:
                if event.type == vad.VADEventType.END_OF_SPEECH:
                    self._tap.add_frames(event.frames)
                self._event_ch.send_nowait(event)
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            with contextlib.suppress(Exception):
                await self._inner_stream.aclose()


class InterviewAgent(voice.Agent):
    def __init__(self, *, instructions: str, graph_llm: LangGraphLLM, tap: _SpeechTap) -> None:
        super().__init__(instructions=instructions)
        self._graph_llm = graph_llm
        self._tap = tap

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        del turn_ctx
        if not delivery_metrics_enabled():
            return
        try:
            audio = self._tap.take()
            transcript = ""
            if new_message is not None and getattr(new_message, "text_content", None):
                transcript = new_message.text_content
            if audio is None:
                delivery = TurnDelivery(missing=True)
            else:
                samples, sample_rate = audio
                delivery = measure_turn(samples, sample_rate, transcript)
            self._graph_llm.add_delivery(delivery)
        except Exception:
            logger.warning("表达特征未能记下，这一轮按缺测继续面试", exc_info=True)
            with contextlib.suppress(Exception):
                self._graph_llm.add_delivery(TurnDelivery(missing=True))


def _frame_to_mono(frame) -> tuple[np.ndarray, int]:
    raw = np.array(frame.data, dtype=np.int16, copy=True)
    channels = int(getattr(frame, "num_channels", 1) or 1)
    if channels > 1:
        usable = raw.size - (raw.size % channels)
        raw = raw[:usable].reshape(-1, channels).mean(axis=1)
    return raw.astype(np.float64) / 32768.0, int(frame.sample_rate)


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if samples.size == 0 or source_rate == target_rate:
        return samples
    duration = samples.size / source_rate
    target_count = max(1, int(round(duration * target_rate)))
    source_x = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    target_x = np.linspace(0.0, 1.0, target_count, endpoint=False)
    return np.interp(target_x, source_x, samples).astype(np.float64)


def _maybe_tap_vad(inner, tap: _SpeechTap):
    if not delivery_metrics_enabled():
        return inner
    if not hasattr(inner, "stream") or getattr(inner, "capabilities", None) is None:
        return inner
    return _TapVAD(inner, tap)


def _build_session(
    participant_identity: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    interview_mode: str | None = None,
    thread_id: str | None = None,
) -> tuple[voice.Agent, voice.AgentSession]:
    user_id = _safe_identity(participant_identity)
    resolved_mode = normalize_interview_mode(interview_mode or config.interview_mode)
    resolved_thread = thread_id or user_id
    logger.info(
        "Initializing LangGraph interview session for thread: %s (mode: %s)",
        resolved_thread,
        resolved_mode,
    )

    graph_llm = LangGraphLLM(
        thread_id=resolved_thread,
        on_event=on_event,
        interview_mode=resolved_mode,
    )
    tap = _SpeechTap()
    agent_instance = InterviewAgent(
        instructions=config.agent_instructions,
        graph_llm=graph_llm,
        tap=tap,
    )
    speech_api_key = resolve_api_key(config.siliconflow_api_key)
    session = voice.AgentSession(
        # 每次调用图都会推进面试状态，投机性的预生成可能把一轮消耗两次。
        # 正确性优先于一点点延迟收益。
        preemptive_generation=False,
        vad=_maybe_tap_vad(
            silero.VAD.load(
                min_speech_duration=0.6,
                min_silence_duration=1.5,
            ),
            tap,
        ),
        stt=openai.STT(
            base_url=config.siliconflow_base_url,
            api_key=speech_api_key,
            model=config.stt_model,
            language=config.stt_language,
        ),
        llm=graph_llm,
        tts=SiliconFlowTTS(
            base_url=config.siliconflow_base_url,
            api_key=speech_api_key,
            model=config.tts_model,
            voice=config.tts_voice,
            response_format=config.tts_response_format,
        ),
    )
    return agent_instance, session


async def entrypoint(ctx: JobContext) -> None:
    try:
        assert_runtime_ready()
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise
    logger.info("Received interview job for room: %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("Connected to room, waiting for participant...")

    participant = await ctx.wait_for_participant()
    participant_identity = participant.identity or f"candidate-{uuid.uuid4().hex[:6]}"
    interview_mode = _resolve_interview_mode(participant)
    session_id = _resolve_session_id(participant)
    thread_id = session_id or f"{ctx.room.name}:{_safe_identity(participant_identity)}"
    logger.info(
        "Participant joined: %s (interview mode: %s, thread: %s)",
        participant_identity,
        interview_mode,
        thread_id,
    )

    async def publish_event(event: dict[str, Any]) -> None:
        """通过数据通道把界面事件（阶段 / 状态 / 成绩单）推给浏览器。"""
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
        thread_id=thread_id,
    )

    logger.info("Starting voice session")
    await session.start(agent=agent_instance, room=ctx.room)

    # 告诉浏览器初始步骤和模式；后续阶段由 LLM 适配器在面试推进时推送。
    await publish_event({"type": "mode", "mode": interview_mode})
    await publish_event({"type": "phase", "phase": "self_intro"})

    # 直接说出开场白。以前把它送进图里，需要在 LLM 适配器里匹配指令文本
    # （“打招呼”），所以一改 GREETING_TEXT 开场就会静默失效。``say`` 也让
    # 开场白留在面试状态机之外——它不是考生的回答。
    logger.info("Speaking opening greeting")
    session.say(config.greeting_text, allow_interruptions=False)


if __name__ == "__main__":
    try:
        assert_runtime_ready()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        logger.warning(
            "You started the agent with `dev`. This only registers a worker and waits for a job assignment. "
            "For local room testing, use `python agent.py connect --room ai-room` so the interviewer joins the room immediately."
        )
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

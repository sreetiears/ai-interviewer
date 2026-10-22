import asyncio
import json
import logging
import os
import re
import sys
import uuid
from typing import Any

import aiohttp
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm, voice
from livekit.agents._exceptions import APIConnectionError, APIStatusError, APITimeoutError, create_api_error_from_http
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr
from livekit.plugins import openai, silero
from livekit.plugins.openai import tts as openai_tts_impl

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ai-interviewer")

DEFAULT_DIFY_API_KEY = "app-gB9GjjboOE2tvMf9m6EL2eEz"
DEFAULT_DIFY_BASE_URL = "http://127.0.0.1/v1"
DEFAULT_SILICONFLOW_API_KEY = "sk-REDACTED"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_STT_MODEL = "FunAudioLLM/SenseVoiceSmall"
DEFAULT_TTS_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
DEFAULT_TTS_VOICE = "FunAudioLLM/CosyVoice2-0.5B:alex"
DEFAULT_TTS_RESPONSE_FORMAT = "mp3"
DEFAULT_AGENT_INSTRUCTIONS = (
    "\u4f60\u662f\u4e00\u540d\u4e2d\u6587\u590d\u8bd5 AI \u9762\u8bd5\u5b98\u3002"
    "\u8bed\u6c14\u81ea\u7136\u3001\u4e13\u4e1a\u3001\u53cb\u597d\u3002"
    "\u6bcf\u6b21\u53ea\u95ee\u4e00\u4e2a\u95ee\u9898\uff0c\u5148\u8ba9\u8003\u751f\u5b8c\u6210\u81ea\u6211\u4ecb\u7ecd\uff0c"
    "\u518d\u6839\u636e\u56de\u7b54\u7ee7\u7eed\u8ffd\u95ee\u3002"
)
DEFAULT_OPENING_PROMPT = (
    "\u8bf7\u5148\u5411\u8003\u751f\u6253\u62db\u547c\uff0c"
    "\u5e76\u9080\u8bf7\u5bf9\u65b9\u5f00\u59cb\u81ea\u6211\u4ecb\u7ecd\u3002"
)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


def _safe_identity(identity: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", identity).strip("-")
    return normalized or f"candidate-{uuid.uuid4().hex[:6]}"


def _message_to_text(message: llm.ChatMessage) -> str:
    if message.text_content:
        return message.text_content.strip()

    parts: list[str] = []
    for item in message.content:
        if isinstance(item, str):
            text = item.strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _extract_query(chat_ctx: llm.ChatContext) -> str:
    latest_fallback = ""

    for message in reversed(chat_ctx.messages()):
        text = _message_to_text(message)
        if not text:
            continue
        if not latest_fallback:
            latest_fallback = text
        if message.role == "user":
            return text

    return latest_fallback or "\u8bf7\u5f00\u59cb\u672c\u8f6e\u9762\u8bd5\u3002"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _extract_usage(payload: dict[str, Any]) -> llm.CompletionUsage | None:
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            raw_usage = metadata.get("usage")

    if not isinstance(raw_usage, dict):
        return None

    prompt_tokens = _safe_int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens"))
    completion_tokens = _safe_int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens"))
    total_tokens = _safe_int(raw_usage.get("total_tokens"))

    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
        return None

    return llm.CompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "msg", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(body, str) and body.strip():
        return body.strip()
    return "Dify request failed"


async def _read_response_body(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except Exception:
        return await response.text()


class DifyLLM(llm.LLM):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        user_id: str,
        app_label: str = "dify-chat-messages",
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.app_label = app_label
        self.conversation_id: str | None = None
        self.api_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self.app_label

    @property
    def provider(self) -> str:
        return "dify"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> "DifyLLMStream":
        del parallel_tool_calls, tool_choice, extra_kwargs

        return DifyLLMStream(
            llm_instance=self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            query=_extract_query(chat_ctx),
        )

    async def aclose(self) -> None:
        return None


class DifyLLMStream(llm.LLMStream):
    def __init__(
        self,
        *,
        llm_instance: DifyLLM,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
        query: str,
    ) -> None:
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._dify_llm = llm_instance
        self._query = query
        self._task_id: str | None = None
        self._request_id = uuid.uuid4().hex
        self._console_stream_started = False

    async def _run(self) -> None:
        if "打招呼" in self._query or "自我介绍" in self._query or "请开始本轮面试" in self._query:
            logger.info("⚡ 拦截到开场白请求，绕过 Dify 本地秒回！")
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=self._request_id,
                    delta=llm.ChoiceDelta(role="assistant",
                                          content="同学你好，欢迎参加考研复试模拟面试，请先进行1分钟左右的自我介绍。"),
                )
            )
            return

        payload: dict[str, Any] = {
            "inputs": {},
            "query": self._query,
            "response_mode": "streaming",
            "user": self._dify_llm.user_id,
        }
        if self._dify_llm.conversation_id:
            payload["conversation_id"] = self._dify_llm.conversation_id

        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._conn_options.timeout,
            sock_connect=self._conn_options.timeout,
            sock_read=None,
        )
        headers = {
            "Authorization": f"Bearer {self._dify_llm.api_key}",
            "Content-Type": "application/json",
        }
        chat_url = f"{self._dify_llm.base_url}/chat-messages"

        try:
            async with self._dify_llm.api_lock:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(chat_url, headers=headers, json=payload) as response:
                        if response.status != 200:
                            body = await _read_response_body(response)
                            raise create_api_error_from_http(
                                _error_message(body),
                                status=response.status,
                                body=body,
                            )

                        async for raw_line in response.content:
                            await self._handle_stream_line(raw_line)

        except asyncio.CancelledError:
            await self._stop_dify()
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise APITimeoutError("Dify request timed out.") from exc
        except asyncio.TimeoutError as exc:
            raise APITimeoutError("Dify request timed out.") from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError(f"Dify connection error: {exc}") from exc

    async def _handle_stream_line(self, raw_line: bytes) -> None:
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line or not line.startswith("data:"):
            return

        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            return

        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("Skip non-json Dify stream chunk: %s", data_str)
            return

        task_id = payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            self._task_id = task_id
            self._request_id = task_id

        conversation_id = payload.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self._dify_llm.conversation_id = conversation_id

        event = payload.get("event")
        if event == "error":
            message = _error_message(payload)
            status_code = _safe_int(payload.get("status")) or 500
            raise APIStatusError(message, status_code=status_code, body=payload, retryable=False)

        answer = payload.get("answer")
        if isinstance(answer, str) and answer:
            if not self._console_stream_started:
                print("\n[Dify] ", end="", flush=True)
                self._console_stream_started = True
            print(answer, end="", flush=True)
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=self._request_id,
                    delta=llm.ChoiceDelta(role="assistant", content=answer),
                )
            )

        if event == "message_end":
            if self._console_stream_started:
                print(flush=True)
                self._console_stream_started = False
            usage = _extract_usage(payload)
            if usage is not None:
                self._event_ch.send_nowait(llm.ChatChunk(id=self._request_id, usage=usage))

    async def _stop_dify(self) -> None:
        if not self._task_id:
            return

        stop_url = f"{self._dify_llm.base_url}/chat-messages/{self._task_id}/stop"
        headers = {
            "Authorization": f"Bearer {self._dify_llm.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=3)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await session.post(
                    stop_url,
                    headers=headers,
                    json={"user": self._dify_llm.user_id},
                )
        except Exception:
            logger.debug("Failed to stop Dify task %s", self._task_id)


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


def _build_session(participant_identity: str) -> tuple[voice.Agent, voice.AgentSession]:
    # 【暴力硬编码】：删掉 _env，彻底无视系统的环境变量，强制使用我们上面写死的配置！
    dify_api_key = DEFAULT_DIFY_API_KEY
    dify_base_url = DEFAULT_DIFY_BASE_URL

    siliconflow_api_key = _env("SILICONFLOW_API_KEY", DEFAULT_SILICONFLOW_API_KEY)
    siliconflow_base_url = _env("SILICONFLOW_BASE_URL", DEFAULT_SILICONFLOW_BASE_URL)
    agent_instructions = _env("AGENT_INSTRUCTIONS", DEFAULT_AGENT_INSTRUCTIONS)
    stt_model = _env("STT_MODEL", DEFAULT_STT_MODEL)
    tts_model = _env("TTS_MODEL", DEFAULT_TTS_MODEL)
    tts_voice = _env("TTS_VOICE", DEFAULT_TTS_VOICE)
    tts_response_format = _env("TTS_RESPONSE_FORMAT", DEFAULT_TTS_RESPONSE_FORMAT)
    stt_language = _env("STT_LANGUAGE", "zh")

    if not dify_api_key:
        raise RuntimeError("DIFY_API_KEY is required.")
    if not siliconflow_api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is required for STT/TTS.")

    user_id = _env("DIFY_USER_ID", _safe_identity(participant_identity))
    dify_llm = DifyLLM(
        api_key=dify_api_key,
        base_url=dify_base_url,
        user_id=user_id,
    )

    agent_instance = voice.Agent(instructions=agent_instructions)
    session = voice.AgentSession(
        # 【加回 VAD 防护】：防止你呼吸或背景杂音把考官打断
        vad=silero.VAD.load(
            min_speech_duration=0.6,
            min_silence_duration=1.5
        ),
        stt=openai.STT(
            base_url=siliconflow_base_url,
            api_key=siliconflow_api_key,
            model=stt_model,
            language=stt_language,
        ),
        llm=dify_llm,
        tts=SiliconFlowTTS(
            base_url=siliconflow_base_url,
            api_key=siliconflow_api_key,
            model=tts_model,
            voice=tts_voice,
            response_format=tts_response_format,
        ),
    )
    return agent_instance, session


async def entrypoint(ctx: JobContext) -> None:
    logger.info("Received interview job for room: %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("Connected to room, waiting for participant...")

    participant = await ctx.wait_for_participant()
    participant_identity = participant.identity or f"candidate-{uuid.uuid4().hex[:6]}"
    logger.info("Participant joined: %s", participant_identity)

    agent_instance, session = _build_session(participant_identity)

    logger.info("Starting voice session with remote Dify backend: %s", _env("DIFY_BASE_URL", DEFAULT_DIFY_BASE_URL))
    await session.start(agent=agent_instance, room=ctx.room)

    await asyncio.sleep(1)
    opening_prompt = _env("OPENING_PROMPT", DEFAULT_OPENING_PROMPT)
    logger.info("Triggering opening message...")
    session.generate_reply(
        user_input=opening_prompt,
        allow_interruptions=False,
        input_modality="text",
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        logger.warning(
            "You started the agent with `dev`. This only registers a worker and waits for a job assignment. "
            "For local room testing, use `python3 agent.py connect --room ai-room` so the interviewer joins the room immediately."
        )
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

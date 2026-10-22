import asyncio
import logging
import os
import re
import sys
import uuid
from typing import Any

import aiohttp
from dotenv import load_dotenv
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm, voice
from livekit.agents._exceptions import APIConnectionError, APIStatusError, APITimeoutError, create_api_error_from_http
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr
from livekit.plugins import openai, silero
from livekit.plugins.openai import tts as openai_tts_impl

# Load environment configuration
load_dotenv()
from config import config
from langgraph_adapter import LangGraphLLM

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ai-interviewer")


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
    for message in reversed(chat_ctx.messages):
        text = _message_to_text(message)
        if not text:
            continue
        if message.role == "user":
            return text
        if not latest_fallback:
            latest_fallback = text

    return latest_fallback or "请开始本轮面试。"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _extract_usage(payload: dict[str, Any]) -> llm.CompletionUsage | None:
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        return None

    prompt_tokens = _safe_int(
        raw_usage.get("prompt_tokens", raw_usage.get("prompt_token_count", 0))
    )
    completion_tokens = _safe_int(
        raw_usage.get("completion_tokens", raw_usage.get("completion_token_count", 0))
    )
    total_tokens = _safe_int(
        raw_usage.get("total_tokens", raw_usage.get("total_token_count", 0))
    )
    if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
        return None

    return llm.CompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or (prompt_tokens + completion_tokens),
    )


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    if isinstance(body, str) and body.strip():
        return body.strip()
    return "Dify request failed"


async def _read_response_body(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except Exception:
        return await response.text()


class DifyLLM(llm.LLM):
    """Legacy Dify backend adapter."""
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
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            return

        body = line[len("data:"):].strip()
        if not body:
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("Failed to decode SSE JSON: %s", body)
            return

        event = data.get("event")
        task_id = data.get("task_id")
        if task_id and not self._task_id:
            self._task_id = str(task_id)

        conversation_id = data.get("conversation_id")
        if conversation_id and not self._dify_llm.conversation_id:
            self._dify_llm.conversation_id = str(conversation_id)

        if event in ("message", "agent_message"):
            answer = data.get("answer", "")
            if answer:
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id=self._request_id,
                        delta=llm.ChoiceDelta(role="assistant", content=answer),
                    )
                )
        elif event == "message_end":
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=self._request_id,
                    usage=_extract_usage(data),
                )
            )
        elif event == "error":
            message = _error_message(data)
            status = _safe_int(data.get("status", 500)) or 500
            raise APIStatusError(message, status=status, body=data)

    async def _stop_dify(self) -> None:
        if not self._task_id:
            return

        url = f"{self._dify_llm.base_url}/chat-messages/{self._task_id}/stop"
        headers = {
            "Authorization": f"Bearer {self._dify_llm.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"user": self._dify_llm.user_id}
        timeout = aiohttp.ClientTimeout(total=5)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        logger.debug("Failed to stop Dify task %s, status=%d", self._task_id, response.status)
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
    # Select LLM backend based on config
    backend = config.llm_backend.lower()
    user_id = _safe_identity(participant_identity)

    if backend == "langgraph":
        logger.info("Initializing LangGraph LLM backend for thread: %s", user_id)
        selected_llm = LangGraphLLM(thread_id=user_id)
    else:
        logger.info("Initializing Dify LLM backend for user: %s (base_url: %s)", user_id, config.dify_base_url)
        if not config.dify_api_key:
            raise RuntimeError("DIFY_API_KEY environment variable is required when LLM_BACKEND=dify")
        selected_llm = DifyLLM(
            api_key=config.dify_api_key,
            base_url=config.dify_base_url,
            user_id=user_id,
        )

    agent_instance = voice.Agent(instructions=config.agent_instructions)
    session = voice.AgentSession(
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
        llm=selected_llm,
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
    logger.info("Participant joined: %s", participant_identity)

    agent_instance, session = _build_session(participant_identity)

    logger.info("Starting voice session (backend: %s)", config.llm_backend)
    await session.start(agent=agent_instance, room=ctx.room)

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

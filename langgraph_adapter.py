import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr

from config import config
from interview_graph import interview_graph
from interview_modes import normalize_mode as normalize_interview_mode

logger = logging.getLogger("langgraph-llm-adapter")

InterviewEvent = dict[str, Any]
EventSink = Callable[[InterviewEvent], Awaitable[None] | None]


class LangGraphLLM(llm.LLM):
    """
    LiveKit LLM adapter that drives the multi-turn interview using LangGraph.
    It fulfills the livekit.agents.llm.LLM interface seamlessly.

    ``on_event`` receives UI-facing events (thinking state, interview phase and the
    final score report) so the browser can render the stepper and score card without
    having to guess from the audio stream.

    ``interview_mode`` selects the question budget: ``quick`` (short run) or
    ``real`` (full-length interview).
    """

    def __init__(
        self,
        thread_id: str | None = None,
        on_event: EventSink | None = None,
        interview_mode: str | None = None,
    ) -> None:
        super().__init__()
        self.thread_id = thread_id or str(uuid.uuid4())
        self.graph = interview_graph
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self.api_lock = asyncio.Lock()
        self._on_event = on_event
        self.interview_mode = normalize_interview_mode(interview_mode or config.interview_mode)
        # The graph checkpoints state between turns, so ``report_data`` and
        # ``current_phase`` survive into later turns.  Remember what the UI has
        # already received or the closing report would pop up again every time
        # the candidate says something after the interview ended.
        self._last_phase: str | None = None
        self._report_emitted = False

    async def emit_event(self, event: InterviewEvent) -> None:
        """Best-effort emit; a broken UI sink must never break the interview."""
        sink = self._on_event
        if sink is None:
            return
        try:
            result = sink(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "Failed to publish interview event %r", event.get("type"), exc_info=True
            )

    def should_emit_phase(self, phase: str) -> bool:
        """True the first time a phase is observed in this session."""
        if not phase or phase == self._last_phase:
            return False
        self._last_phase = phase
        return True

    def should_emit_report(self) -> bool:
        """True at most once per session; checkpoints retain the report forever."""
        if self._report_emitted:
            return False
        self._report_emitted = True
        return True

    @property
    def model(self) -> str:
        return "langgraph-interview-orchestrator"

    @property
    def provider(self) -> str:
        return "langgraph"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> "LangGraphLLMStream":
        del tools, parallel_tool_calls, tool_choice, extra_kwargs

        query = self._extract_query(chat_ctx)
        return LangGraphLLMStream(
            llm_instance=self,
            chat_ctx=chat_ctx,
            tools=[],
            conn_options=conn_options,
            query=query,
        )

    async def aclose(self) -> None:
        return None

    def _extract_query(self, chat_ctx: llm.ChatContext) -> str:
        """Return the latest user utterance, or ``""`` when there is none.

        Never fall back to an assistant message: the graph treats its input as a
        candidate answer, so feeding it the examiner's previous question would
        advance the interview state with the wrong speaker.
        """
        for msg in reversed(chat_ctx.messages()):
            if msg.role != "user":
                continue
            if msg.text_content:
                return msg.text_content.strip()
            parts = [p.strip() for p in msg.content if isinstance(p, str) and p.strip()]
            if parts:
                return "\n".join(parts).strip()
        return ""


class LangGraphLLMStream(llm.LLMStream):
    def __init__(
        self,
        *,
        llm_instance: LangGraphLLM,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
        query: str,
    ) -> None:
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._graph_llm = llm_instance
        self._query = query
        self._request_id = uuid.uuid4().hex

    async def _run(self) -> None:
        if not self._query:
            # LiveKit can ask for a reply without a user message (for example an
            # empty turn trigger).  There is nothing to answer, and inventing a
            # query would corrupt the interview state.
            logger.info("LangGraphLLMStream: no user utterance, skipping graph")
            return

        graph_llm = self._graph_llm
        await graph_llm.emit_event({"type": "status", "state": "thinking"})

        async with graph_llm.api_lock:
            streamed_any = False
            result: dict[str, Any] = {}
            try:
                input_state = {
                    "user_query": self._query,
                    "interview_mode": graph_llm.interview_mode,
                }
                # "custom" carries per-token deltas written by graph nodes, so TTS can
                # start on the first token; "values" carries the final state.
                async for mode, chunk in graph_llm.graph.astream(
                    input_state,
                    config=graph_llm.config,
                    stream_mode=["custom", "values"],
                ):
                    if mode == "custom":
                        if isinstance(chunk, dict) and chunk.get("type") == "delta":
                            text = chunk.get("text") or ""
                            if text:
                                streamed_any = True
                                self._send_chunk(text)
                    elif mode == "values" and isinstance(chunk, dict):
                        result = chunk

                reply_text = str(result.get("reply") or "好的，请继续。")
                logger.info("[LangGraphLLMStream] Output reply: %s", reply_text[:60])

                if not streamed_any:
                    # Static replies (e.g. phase transitions) never enter the token
                    # stream; send them whole so TTS can start immediately.
                    self._send_chunk(reply_text)

                phase = result.get("current_phase")
                if graph_llm.should_emit_phase(phase):
                    await graph_llm.emit_event({"type": "phase", "phase": phase})

                report_data = result.get("report_data")
                final_report = str(result.get("final_report") or "")
                has_structured_report = isinstance(report_data, dict) and bool(report_data)
                # Even when the model returned unparseable JSON, the graph keeps
                # the raw text in ``final_report``.  Sending it lets the browser
                # show the raw report instead of silently showing nothing.
                if (has_structured_report or final_report) and graph_llm.should_emit_report():
                    await graph_llm.emit_event({
                        "type": "report",
                        "report": report_data if has_structured_report else {},
                        "text": final_report,
                    })

                await graph_llm.emit_event({"type": "status", "state": "speaking"})

            except Exception as e:
                logger.error("[LangGraphLLMStream] Error executing graph: %s", e, exc_info=True)
                await graph_llm.emit_event({"type": "status", "state": "listening"})
                self._send_chunk("系统处理出现异常，请稍后再试。")

    def _send_chunk(self, text: str) -> None:
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id=self._request_id,
                delta=llm.ChoiceDelta(role="assistant", content=text),
            )
        )

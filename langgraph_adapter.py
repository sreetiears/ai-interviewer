import asyncio
import inspect
import logging
import uuid
from dataclasses import asdict
from collections.abc import Awaitable, Callable
from typing import Any
from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr

from config import config, delivery_metrics_enabled
from interview_graph import interview_graph
from perception.delivery import TurnDelivery
from interview_modes import normalize_mode as normalize_interview_mode

logger = logging.getLogger("langgraph-llm-adapter")

InterviewEvent = dict[str, Any]
EventSink = Callable[[InterviewEvent], Awaitable[None] | None]


def user_facing_error_message(exc: BaseException) -> str:
    """把图执行异常映射成可播报的短中文，避免台上只听到笼统道歉。"""
    name = type(exc).__name__.lower()
    text = f"{name}: {exc}".lower()
    joined = f"{name} {text}"
    if any(
        token in joined
        for token in ("timeout", "timed out", "deadline", "readtimeout", "connecttimeout")
    ):
        return "考官思考超时了，请再说一遍你的回答。"
    if any(
        token in joined
        for token in (
            "401",
            "403",
            "unauthorized",
            "invalid api key",
            "authentication",
            "api key",
            "permissiondenied",
        )
    ):
        return "语音服务鉴权失败，请检查 API Key 配置后重试。"
    if any(
        token in joined
        for token in ("429", "rate limit", "too many requests", "quota", "ratelimit")
    ):
        return "当前请求过于频繁，请稍等几秒再试。"
    return "系统处理出现异常，请稍后再试。"


class LangGraphLLM(llm.LLM):
    """
    用 LangGraph 驱动多轮面试的 LiveKit LLM 适配器。
    它无缝实现 livekit.agents.llm.LLM 接口。

    ``on_event`` 接收面向界面的事件（思考状态、面试阶段和最终成绩单），
    浏览器据此渲染步进器和成绩卡，而不必从音频流里猜测。

    ``interview_mode`` 选择题目额度：``quick``（短流程）或
    ``real``（完整时长的面试）。
    """

    def __init__(
        self,
        thread_id: str | None = None,
        on_event: EventSink | None = None,
        interview_mode: str | None = None,
        learner_id: str | None = None,
        focus_topics: list[str] | None = None,
        profile_snapshot: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self.thread_id = thread_id or str(uuid.uuid4())
        self.graph = interview_graph
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self.api_lock = asyncio.Lock()
        self._on_event = on_event
        self.interview_mode = normalize_interview_mode(interview_mode or config.interview_mode)
        self.learner_id = (learner_id or "").strip()
        self.focus_topics = [str(t).strip() for t in (focus_topics or []) if str(t).strip()]
        self.profile_snapshot = str(profile_snapshot or "").strip()
        self.session_id = (session_id or self.thread_id).strip()
        # 图会在轮次之间把状态写入检查点，因此 ``report_data`` 和
        # ``current_phase`` 会留到后续轮次。记住界面已经收到过什么，
        # 否则面试结束后考生再说一句话，结束成绩单就会再弹一次。
        self._last_phase: str | None = None
        self._report_emitted = False
        self._delivery_turns: list[TurnDelivery] = []

    async def emit_event(self, event: InterviewEvent) -> None:
        """尽力发送；界面接收端坏了也绝不能打断面试。"""
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
        """本会话第一次观察到该阶段时返回 True。"""
        if not phase or phase == self._last_phase:
            return False
        self._last_phase = phase
        return True

    def add_delivery(self, turn: TurnDelivery) -> None:
        """记下这一轮的表达特征。波形不进入这里。"""
        self._delivery_turns.append(turn)

    def should_emit_report(self) -> bool:
        """每个会话最多返回一次 True；检查点会一直保留成绩单。"""
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
        """返回最近一条用户发言；没有时返回 ``""``。

        绝不能回退到助手消息：图把输入当作考生回答，若喂进考官上一句提问，
        就会用错误的说话人推进面试状态。
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
            # LiveKit 可能在没有用户消息时请求回复（例如一次空轮触发）。
            # 此时没有内容可答，编造一条查询会破坏面试状态。
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
                    "learner_id": graph_llm.learner_id,
                    "session_id": graph_llm.session_id,
                    "focus_topics": ",".join(graph_llm.focus_topics),
                    "profile_snapshot": graph_llm.profile_snapshot,
                }
                if delivery_metrics_enabled():
                    input_state["delivery_turns"] = [
                        asdict(turn) for turn in graph_llm._delivery_turns
                    ]
                # "custom" 携带图节点写出的逐 token 增量，语音合成可以从第一个
                # token 开始；"values" 携带最终状态。
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
                    # 静态回复（例如阶段过渡）不会进入 token 流；整段发出，
                    # 让语音合成可以立刻开始。
                    self._send_chunk(reply_text)

                phase = result.get("current_phase")
                if graph_llm.should_emit_phase(phase):
                    await graph_llm.emit_event({"type": "phase", "phase": phase})

                report_data = result.get("report_data")
                final_report = str(result.get("final_report") or "")
                has_structured_report = isinstance(report_data, dict) and bool(report_data)
                # 即使模型返回了无法解析的 JSON，图仍把原文留在 ``final_report``。
                # 把它发出去，浏览器就能显示原始成绩单，而不是什么都不显示。
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
                self._send_chunk(user_facing_error_message(e))

    def _send_chunk(self, text: str) -> None:
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id=self._request_id,
                delta=llm.ChoiceDelta(role="assistant", content=text),
            )
        )

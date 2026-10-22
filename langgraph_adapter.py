import asyncio
import logging
import uuid
from typing import Any
from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr

from interview_graph import interview_graph

logger = logging.getLogger("langgraph-llm-adapter")


class LangGraphLLM(llm.LLM):
    """
    LiveKit LLM adapter that drives the multi-turn interview using LangGraph.
    It fulfills the livekit.agents.llm.LLM interface seamlessly.
    """
    def __init__(self, thread_id: str | None = None) -> None:
        super().__init__()
        self.thread_id = thread_id or str(uuid.uuid4())
        self.graph = interview_graph
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self.api_lock = asyncio.Lock()

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
        latest = ""
        for msg in reversed(chat_ctx.messages):
            if msg.role == "user":
                if msg.text_content:
                    return msg.text_content.strip()
                parts = [p.strip() for p in msg.content if isinstance(p, str) and p.strip()]
                if parts:
                    return "\n".join(parts).strip()
            elif msg.role == "assistant" and not latest:
                latest = msg.text_content or ""
        return latest or "请开始本轮面试。"


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
        # Fast interception for initial greeting
        if "打招呼" in self._query or "开始自我介绍" in self._query:
            logger.info("⚡ LangGraphLLMStream: Intercepted opening greeting.")
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=self._request_id,
                    delta=llm.ChoiceDelta(
                        role="assistant",
                        content="同学你好，欢迎参加考研复试模拟面试，请先进行1分钟左右的自我介绍。",
                    ),
                )
            )
            return

        async with self._graph_llm.api_lock:
            try:
                # Invoke the LangGraph workflow
                input_state = {"user_query": self._query}
                result = await self._graph_llm.graph.ainvoke(
                    input_state,
                    config=self._graph_llm.config,
                )

                reply_text = result.get("reply", "好的，请继续。")
                logger.info("[LangGraphLLMStream] Output reply: %s", reply_text[:60])

                # Stream the reply in chunks for speech synthesis
                chunk_size = 12
                for i in range(0, len(reply_text), chunk_size):
                    chunk = reply_text[i:i + chunk_size]
                    self._event_ch.send_nowait(
                        llm.ChatChunk(
                            id=self._request_id,
                            delta=llm.ChoiceDelta(role="assistant", content=chunk),
                        )
                    )
                    await asyncio.sleep(0.01)

            except Exception as e:
                logger.error("[LangGraphLLMStream] Error executing graph: %s", e, exc_info=True)
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id=self._request_id,
                        delta=llm.ChoiceDelta(role="assistant", content="系统处理出现异常，请稍后再试。"),
                    )
                )

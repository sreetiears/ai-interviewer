"""查询侧构造与面试出题用的检索入口。"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from learner_store import TOPIC_LABELS, parse_focus_topics
from rag.embeddings import Embedder
from rag.index import KnowledgeIndex, RetrievedChunk, get_index

logger = logging.getLogger("rag-retrieve")


def _normalize_topics(focus_topics: Sequence[str] | str | None) -> list[str]:
    """``parse_focus_topics`` 只吃字符串；列表先拼成逗号串。"""
    if focus_topics is None:
        return []
    if isinstance(focus_topics, (list, tuple, set)):
        return parse_focus_topics(",".join(str(t) for t in focus_topics))
    return parse_focus_topics(focus_topics)


def build_query(
    focus_topics: Sequence[str] | str | None = None,
    profile_snapshot: str | None = None,
) -> str:
    """文档 A：薄弱点 / 加强方向 + 档案摘要 → 检索查询文本。"""
    topics = _normalize_topics(focus_topics)
    parts: list[str] = []
    if topics:
        labels = [TOPIC_LABELS.get(t, t) for t in topics]
        parts.append("优先加强知识点：" + "、".join(labels))
        parts.append("topics: " + ", ".join(topics))
    snapshot = str(profile_snapshot or "").strip()
    if snapshot:
        parts.append(snapshot)
    return "\n".join(parts).strip()


def format_passages_for_prompt(chunks: Sequence[RetrievedChunk]) -> str:
    if not chunks:
        return ""
    lines = ["知识库参考（据此出题，勿照抄原文）："]
    for i, chunk in enumerate(chunks, start=1):
        label = TOPIC_LABELS.get(chunk.topic, chunk.topic) or chunk.source or "片段"
        lines.append(f"[{i}] ({label}) {chunk.text.strip()}")
    return "\n".join(lines)


def retrieve_for_interview(
    *,
    focus_topics: Sequence[str] | str | None = None,
    profile_snapshot: str | None = None,
    top_k: int | None = None,
    index: KnowledgeIndex | None = None,
    embedder: Embedder | None = None,
) -> list[RetrievedChunk]:
    """为专业课出题检索知识库；失败返回空列表（调用方降级）。"""
    from config import config, rag_enabled

    if not rag_enabled():
        return []
    query = build_query(focus_topics, profile_snapshot)
    if not query:
        return []
    topics = _normalize_topics(focus_topics)
    k = int(top_k if top_k is not None else config.rag_top_k)
    try:
        store = index or get_index(embedder=embedder)
        return store.query(query, top_k=k, topics=topics or None)
    except Exception:
        logger.warning("RAG retrieve_for_interview failed", exc_info=True)
        return []


def match_documents(
    query: str,
    *,
    topics: Sequence[str] | str | None = None,
    top_k: int | None = None,
    index: KnowledgeIndex | None = None,
    embedder: Embedder | None = None,
) -> list[dict[str, Any]]:
    """通用两文档匹配：用 query（文档 A）去匹配知识库（文档 B）。"""
    from config import config, rag_enabled

    if not rag_enabled():
        return []
    text = str(query or "").strip()
    if not text:
        return []
    topic_list = _normalize_topics(topics)
    k = int(top_k if top_k is not None else config.rag_top_k)
    try:
        store = index or get_index(embedder=embedder)
        chunks = store.query(text, top_k=k, topics=topic_list or None)
    except Exception:
        logger.warning("RAG match_documents failed", exc_info=True)
        return []
    return [
        {
            "text": c.text,
            "topic": c.topic,
            "source": c.source,
            "score": round(c.score, 4),
            "chunk_id": c.chunk_id,
        }
        for c in chunks
    ]


def rag_context_for_prompt(
    state_focus_topics: Sequence[str] | str | None,
    profile_snapshot: str | None,
    *,
    index: KnowledgeIndex | None = None,
) -> str:
    """出题提示词用的参考段落；任何异常都变成空串。"""
    chunks = retrieve_for_interview(
        focus_topics=state_focus_topics,
        profile_snapshot=profile_snapshot,
        index=index,
    )
    return format_passages_for_prompt(chunks)

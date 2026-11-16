"""Chroma 持久化索引：建库、加载、按 topic 过滤召回。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rag.chunking import KnowledgeChunk, load_knowledge_dir
from rag.embeddings import Embedder, FakeEmbedding

logger = logging.getLogger("rag-index")

COLLECTION_NAME = "interview_knowledge"


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    topic: str
    source: str
    score: float
    chunk_id: str


class KnowledgeIndex:
    """薄封装：不引入 LlamaIndex，只做 ingest / query。"""

    def __init__(
        self,
        *,
        persist_path: str | Path | None,
        embedder: Embedder | None = None,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        import chromadb

        self.embedder = embedder or FakeEmbedding()
        self.collection_name = collection_name
        self.persist_path = str(persist_path) if persist_path else None
        if self.persist_path:
            Path(self.persist_path).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.persist_path)
        else:
            self._client = chromadb.EphemeralClient()
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def count(self) -> int:
        return int(self._collection.count())

    def clear(self) -> None:
        self._client.delete_collection(self.collection_name)
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def ingest_chunks(self, chunks: Sequence[KnowledgeChunk], *, replace: bool = True) -> int:
        if replace:
            self.clear()
        if not chunks:
            return 0
        texts = [c.text for c in chunks]
        embeddings = self.embedder.embed_documents(texts)
        ids = [c.chunk_id for c in chunks]
        metadatas = [{"topic": c.topic, "source": c.source} for c in chunks]
        self._collection.add(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info("Ingested %d chunks into %s", len(chunks), self.collection_name)
        return len(chunks)

    def ingest_directory(self, knowledge_dir: str | Path, *, replace: bool = True) -> int:
        chunks = load_knowledge_dir(knowledge_dir)
        return self.ingest_chunks(chunks, replace=replace)

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 3,
        topics: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        if not query_text.strip() or self.count == 0 or top_k <= 0:
            return []

        embedding = self.embedder.embed_query(query_text)
        where = None
        topic_list = [t.strip() for t in (topics or []) if str(t).strip()]
        if len(topic_list) == 1:
            where = {"topic": topic_list[0]}
        elif len(topic_list) > 1:
            where = {"topic": {"$in": topic_list}}

        kwargs: dict = {
            "query_embeddings": [embedding],
            "n_results": min(top_k, self.count),
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            kwargs["where"] = where

        try:
            raw = self._collection.query(**kwargs)
        except Exception:
            # 过滤过严（例如索引尚未含该 topic）时，回退为全局检索。
            logger.warning("Filtered RAG query failed; falling back to global search", exc_info=True)
            raw = self._collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, self.count),
                include=["documents", "metadatas", "distances"],
            )

        return _parse_query_result(raw)


def _parse_query_result(raw: dict) -> list[RetrievedChunk]:
    documents = (raw.get("documents") or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]
    ids = (raw.get("ids") or [[]])[0]
    out: list[RetrievedChunk] = []
    for i, doc in enumerate(documents):
        meta = metadatas[i] if i < len(metadatas) else {}
        dist = float(distances[i]) if i < len(distances) else 1.0
        # Chroma cosine distance：越小越相近；转成 0–1 左右的相似分便于展示。
        score = max(0.0, 1.0 - dist)
        out.append(
            RetrievedChunk(
                text=str(doc or ""),
                topic=str((meta or {}).get("topic") or ""),
                source=str((meta or {}).get("source") or ""),
                score=score,
                chunk_id=str(ids[i]) if i < len(ids) else "",
            )
        )
    return out


_index_singleton: KnowledgeIndex | None = None


def resolve_default_embedder() -> Embedder:
    """有可用 API Key 时走远程 embedding，否则用 FakeEmbedding（须与建库方式一致）。"""
    from config import allow_dummy_keys, config, is_usable_api_key
    from rag.embeddings import FakeEmbedding, default_embedder_from_config

    llm_ok = is_usable_api_key(config.llm_api_key)
    speech_ok = is_usable_api_key(config.siliconflow_api_key)
    if llm_ok or speech_ok:
        return default_embedder_from_config()
    if allow_dummy_keys():
        return FakeEmbedding()
    return FakeEmbedding()


def get_index(
    *,
    persist_path: str | None = None,
    embedder: Embedder | None = None,
    force_new: bool = False,
) -> KnowledgeIndex:
    """进程内复用持久化索引；测试可传 ``force_new`` 或 ephemeral。"""
    global _index_singleton
    from config import config

    path = persist_path if persist_path is not None else config.rag_persist_path
    if force_new or _index_singleton is None:
        _index_singleton = KnowledgeIndex(
            persist_path=path,
            embedder=embedder or resolve_default_embedder(),
        )
    return _index_singleton


def reset_index_singleton() -> None:
    global _index_singleton
    _index_singleton = None

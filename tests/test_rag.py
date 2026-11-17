"""双文档 RAG：切分、FakeEmbedding 索引、出题注入与 match API。"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from interview_graph import _question_prompt
from rag.chunking import load_knowledge_dir, topic_from_path
from rag.embeddings import FakeEmbedding
from rag.index import KnowledgeIndex, reset_index_singleton
from rag.retrieve import (
    build_query,
    format_passages_for_prompt,
    match_documents,
    rag_context_for_prompt,
    retrieve_for_interview,
)
import server


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = ROOT / "knowledge"


@pytest.fixture()
def rag_index(tmp_path):
    reset_index_singleton()
    index = KnowledgeIndex(persist_path=tmp_path / "chroma", embedder=FakeEmbedding())
    count = index.ingest_directory(KNOWLEDGE_DIR, replace=True)
    assert count >= 10
    yield index
    reset_index_singleton()


def test_topic_from_path_and_knowledge_loads():
    assert topic_from_path(Path("os.memory.md")) == "os.memory"
    chunks = load_knowledge_dir(KNOWLEDGE_DIR)
    topics = {c.topic for c in chunks}
    assert "os.memory" in topics
    assert "cn.tcp" in topics
    assert all(c.text.strip() for c in chunks)


def test_build_query_includes_focus_and_snapshot():
    q = build_query(["os.memory", "cn.tcp"], "历史薄弱点：操作系统·内存")
    assert "os.memory" in q
    assert "操作系统·内存" in q
    assert "计算机网络·TCP" in q or "TCP" in q


def test_retrieve_filters_by_topic(rag_index):
    hits = retrieve_for_interview(
        focus_topics=["os.memory"],
        profile_snapshot="加强：操作系统·内存 虚拟内存 分页",
        index=rag_index,
        top_k=3,
    )
    assert hits
    assert all(h.topic == "os.memory" for h in hits)
    joined = "\n".join(h.text for h in hits)
    assert "虚拟内存" in joined or "页" in joined


def test_match_documents_returns_scores(rag_index):
    matches = match_documents(
        "请说明 TCP 三次握手与可靠传输",
        topics=["cn.tcp"],
        top_k=2,
        index=rag_index,
    )
    assert matches
    assert matches[0]["topic"] == "cn.tcp"
    assert "score" in matches[0]
    assert matches[0]["text"]


def test_question_prompt_injects_cached_rag_context(rag_index, monkeypatch):
    """出题热路径只读 state.rag_context，不触发 get_index / embed。"""
    monkeypatch.setenv("RAG_ENABLED", "1")
    called = {"get_index": 0}

    def boom(**kwargs):
        called["get_index"] += 1
        raise AssertionError("hot path must not call get_index")

    monkeypatch.setattr("rag.retrieve.get_index", boom)
    cached = rag_context_for_prompt(
        ["os.memory"],
        "历史薄弱点：操作系统·内存",
        index=rag_index,
    )
    assert "知识库参考" in cached
    state = {
        "asked_questions": "",
        "focus_topics": "os.memory",
        "profile_snapshot": "历史薄弱点：操作系统·内存",
        "rag_context": cached,
    }
    prompt = _question_prompt("请出一道题", state, include_rag=True)
    assert "知识库参考" in prompt
    assert "操作系统" in prompt or "内存" in prompt or "分页" in prompt
    assert called["get_index"] == 0


def test_question_prompt_skips_rag_when_cache_empty():
    state = {
        "asked_questions": "",
        "focus_topics": "os.memory",
        "profile_snapshot": "历史薄弱点：操作系统·内存",
        "rag_context": "",
    }
    prompt = _question_prompt("请出一道题", state, include_rag=True)
    assert "知识库参考" not in prompt


def test_rag_context_empty_without_query(rag_index, monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setattr("rag.retrieve.get_index", lambda **kwargs: rag_index)
    assert rag_context_for_prompt([], "") == ""
    assert format_passages_for_prompt([]) == ""


@pytest.fixture()
def http_port(frontend_root, monkeypatch):
    """复用 server 测试夹具风格：临时前端 + 真实端口。"""
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.InterviewHttpHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def frontend_root(tmp_path, monkeypatch):
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(server, "FRONTEND_DIR", str(root))
    return root


def test_api_rag_match(http_port, rag_index, monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setattr("rag.retrieve.get_index", lambda **kwargs: rag_index)
    body = json.dumps(
        {
            "query": "虚拟内存分页与缺页",
            "topics": ["os.memory"],
            "top_k": 2,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/rag/match",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        raw = resp.read()
        assert resp.status == 200
        payload = json.loads(raw.decode("utf-8"))
        assert payload["matches"]
        assert payload["matches"][0]["topic"] == "os.memory"
    finally:
        conn.close()


def test_api_rag_match_requires_query(http_port, monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "1")
    body = b'{"query":""}'
    conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/rag/match",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        resp.read()
    finally:
        conn.close()

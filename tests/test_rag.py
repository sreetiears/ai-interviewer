"""双文档 RAG：切分、FakeEmbedding 索引、出题注入与 match API。"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from interview_graph import (
    _REPORT_RAG_NOTE,
    _question_prompt,
    node_compre_qa,
    node_pro_qa,
    node_report,
)
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


def _forbid_retrieval(monkeypatch) -> dict[str, int]:
    """把所有检索入口换成哨兵：热路径若误调用，计数加一并抛错。"""
    calls = {"get_index": 0, "query": 0, "retrieve": 0}

    def sentinel(name: str):
        def _boom(*args, **kwargs):
            calls[name] += 1
            raise AssertionError(f"hot path must not call {name}")

        return _boom

    monkeypatch.setattr("rag.retrieve.get_index", sentinel("get_index"))
    monkeypatch.setattr("rag.index.get_index", sentinel("get_index"))
    monkeypatch.setattr("rag.index.KnowledgeIndex.query", sentinel("query"))
    monkeypatch.setattr("rag.retrieve.retrieve_for_interview", sentinel("retrieve"))
    monkeypatch.setattr("rag.retrieve.match_documents", sentinel("retrieve"))
    monkeypatch.setattr("rag.retrieve.rag_context_for_prompt", sentinel("retrieve"))
    return calls


@pytest.mark.asyncio
async def test_pro_followup_reuses_cached_rag_without_retrieval(rag_index, monkeypatch):
    """专业课追问复用开场缓存：提示词带知识库段落，热路径零次检索。"""
    monkeypatch.setenv("RAG_ENABLED", "1")
    cached = rag_context_for_prompt(["os.memory"], "历史薄弱点：操作系统·内存", index=rag_index)
    assert "知识库参考" in cached
    calls = _forbid_retrieval(monkeypatch)
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="需要"),
        AIMessage(content="缺页中断之后，系统如何找到物理页？"),
    ])
    state = {
        "current_phase": "pro_qa",
        "is_answering": True,
        "question_pro": "请说明虚拟内存的作用。",
        "user_query": "虚拟内存让每个进程拥有独立的地址空间。",
        "follow_up_count": 0,
        "focus_topics": "os.memory",
        "profile_snapshot": "历史薄弱点：操作系统·内存",
        "rag_context": cached,
    }
    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_pro_qa(state)

    assert result["follow_up_count"] == 1
    followup_prompt = mock_llm.ainvoke.await_args_list[1].args[0][1].content
    assert "【原问题】请说明虚拟内存的作用。" in followup_prompt
    assert cached in followup_prompt
    assert sum(calls.values()) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_field", [{}, {"rag_context": ""}], ids=["no-key", "blank"])
async def test_pro_followup_generates_without_rag_cache(cache_field, monkeypatch):
    calls = _forbid_retrieval(monkeypatch)
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="需要"),
        AIMessage(content="能否再说明一下实现原理？"),
    ])
    state = {
        "current_phase": "pro_qa",
        "is_answering": True,
        "question_pro": "请说明虚拟内存的作用。",
        "user_query": "虚拟内存让每个进程拥有独立的地址空间。",
        "follow_up_count": 0,
        **cache_field,
    }
    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_pro_qa(state)

    assert result["follow_up_count"] == 1
    assert result["reply"] == "能否再说明一下实现原理？"
    followup_prompt = mock_llm.ainvoke.await_args_list[1].args[0][1].content
    assert "【原问题】请说明虚拟内存的作用。" in followup_prompt
    assert "知识库参考" not in followup_prompt
    assert sum(calls.values()) == 0


@pytest.mark.asyncio
async def test_pro_main_question_still_gets_cached_rag(rag_index, monkeypatch):
    """专业课主题目照旧注入缓存段落，且不做检索。"""
    monkeypatch.setenv("RAG_ENABLED", "1")
    cached = rag_context_for_prompt(["os.memory"], "历史薄弱点：操作系统·内存", index=rag_index)
    calls = _forbid_retrieval(monkeypatch)
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="请说明虚拟内存的作用。"))
    state = {
        "current_phase": "pro_qa",
        "is_answering": False,
        "question_pro": "",
        "questions_asked": 0,
        "focus_topics": "os.memory",
        "profile_snapshot": "历史薄弱点：操作系统·内存",
        "rag_context": cached,
    }
    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_pro_qa(state)

    assert result["question_pro"] == "请说明虚拟内存的作用。"
    question_prompt = mock_llm.ainvoke.await_args.args[0][1].content
    assert cached in question_prompt
    assert sum(calls.values()) == 0


@pytest.mark.asyncio
async def test_compre_followup_and_questions_never_get_rag_block(rag_index, monkeypatch):
    """综合素质的追问与出题不带知识库段落，也不触发检索。"""
    monkeypatch.setenv("RAG_ENABLED", "1")
    cached = rag_context_for_prompt(["os.memory"], "历史薄弱点：操作系统·内存", index=rag_index)
    assert "知识库参考" in cached
    calls = _forbid_retrieval(monkeypatch)
    state = {
        "current_phase": "compre_qa",
        "is_answering": True,
        "question_compre": "谈谈你遇到的团队冲突。",
        "user_query": "我们一起讨论后解决了。",
        "follow_up_count": 0,
        "questions_asked": 1,
        "questions_per_phase": 3,
        "raw_scores": "",
        "focus_topics": "os.memory",
        "profile_snapshot": "历史薄弱点：操作系统·内存",
        "rag_context": cached,
    }

    followup_llm = MagicMock()
    followup_llm.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="需要"),
        AIMessage(content="能举一个具体事例吗？"),
    ])
    with patch("interview_graph._get_llm", return_value=followup_llm):
        followup = await node_compre_qa(state)
    assert followup["follow_up_count"] == 1
    assert "知识库参考" not in followup_llm.ainvoke.await_args_list[1].args[0][1].content

    next_llm = MagicMock()
    next_llm.ainvoke = AsyncMock(side_effect=[
        AIMessage(content="不需要"),
        AIMessage(content="再说说你的科研经历。"),
    ])
    with patch("interview_graph._get_llm", return_value=next_llm):
        next_result = await node_compre_qa(state)
    assert next_result["questions_asked"] == 2
    assert "知识库参考" not in next_llm.ainvoke.await_args_list[1].args[0][1].content
    assert sum(calls.values()) == 0


@pytest.mark.asyncio
async def test_report_appends_cached_rag_as_profession_reference_only(rag_index, monkeypatch):
    """成绩单把缓存段落当作专业知识参考，并且不触发检索。"""
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setenv("DELIVERY_METRICS", "0")
    cached = rag_context_for_prompt(["os.memory"], "历史薄弱点：操作系统·内存", index=rag_index)
    assert "知识库参考" in cached
    calls = _forbid_retrieval(monkeypatch)
    report_json = (
        '{"total_score": 80, "scores": {"profession_know": 70, "exp_logic": 75, "comper_quality": 80}, '
        '"core_weakness": "例子少", "improvement_tips": "补充例子", "interviewer_closing": "辛苦了"}'
    )
    seen: dict[str, str] = {}

    async def _invoke(messages):
        seen["human"] = messages[1].content
        return AIMessage(content=report_json)

    mock_llm = MagicMock()
    mock_llm.ainvoke = _invoke
    with patch("interview_graph._get_llm", return_value=mock_llm):
        result = await node_report({"raw_scores": "【记录】", "rag_context": cached})

    human = seen["human"]
    assert human.startswith("这是该考生本场面试的完整文字记录：\n【记录】\n")
    assert _REPORT_RAG_NOTE in human
    assert human.index(_REPORT_RAG_NOTE) < human.index(cached)
    assert "profession_know" in _REPORT_RAG_NOTE
    assert "comper_quality" in _REPORT_RAG_NOTE
    assert result["current_phase"] == "completed"
    assert sum(calls.values()) == 0


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

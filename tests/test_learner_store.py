"""考生长期记忆 / 个人知识库的单元测试。"""

import time

import pytest

from learner_store import (
    LearnerStore,
    decayed_mastery,
    normalize_topic,
    normalize_verdict,
    parse_focus_topics,
)


@pytest.fixture()
def store(tmp_path):
    return LearnerStore(tmp_path / "learner_kb.sqlite3")


def test_ensure_learner_and_touch(store):
    store.ensure_learner("learner-aaaabbbbcccc")
    store.ensure_learner("learner-aaaabbbbcccc")
    snapshot = store.build_snapshot("learner-aaaabbbbcccc")
    assert snapshot.learner_id == "learner-aaaabbbbcccc"
    assert snapshot.session_count == 0


def test_apply_verdict_updates_mastery(store):
    learner = "learner-mastery0001"
    after_weak = store.apply_verdict(learner, "os.memory", "weak")
    assert after_weak < 0.5
    after_strong = store.apply_verdict(learner, "os.memory", "strong")
    assert after_strong > after_weak
    scores = store.get_topic_scores(learner)
    assert scores[0].topic == "os.memory"
    assert scores[0].evidence_count == 2


def test_decayed_mastery_halves_after_half_life():
    now = time.time()
    half = decayed_mastery(0.8, now - 30 * 86400, now=now, half_life_days=30)
    assert half == pytest.approx(0.4, abs=1e-6)


def test_record_session_persists_and_updates_focus(store):
    learner = "learner-session0001"
    weak = store.record_session(
        learner_id=learner,
        session_id="sess-1",
        mode="quick",
        report={
            "total_score": 78,
            "core_weakness": "虚拟内存讲不清",
            "improvement_tips": "补操作系统",
            "weak_topics": ["os.memory"],
        },
        turn_records=[
            {"topic": "os.memory", "verdict": "weak", "question": "什么是缺页中断？"},
            {"topic": "cn.tcp", "verdict": "ok", "question": "TCP 三次握手？"},
        ],
        asked_items=[
            ("什么是缺页中断？", "os.memory"),
            ("TCP 三次握手？", "cn.tcp"),
        ],
    )
    assert "os.memory" in weak
    sessions = store.list_sessions(learner, limit=5)
    assert len(sessions) == 1
    assert sessions[0]["sessionId"] == "sess-1"
    assert sessions[0]["coreWeakness"] == "虚拟内存讲不清"
    asked = store.list_recent_asked(learner)
    assert any("缺页" in item for item in asked)
    focus = store.suggest_focus_topics(learner)
    assert "os.memory" in focus


def test_explicit_focus_overrides_auto(store):
    learner = "learner-focus000001"
    store.apply_verdict(learner, "os.memory", "weak")
    focus = store.suggest_focus_topics(learner, explicit=["cn.http", "soft.team"])
    assert focus == ["cn.http", "soft.team"]


def test_kb_facts_upsert_and_list(store):
    learner = "learner-facts000001"
    store.upsert_fact(learner, "habit", "prefers_evening", {"value": True})
    store.upsert_fact(learner, "habit", "prefers_evening", {"value": False, "note": "updated"})
    facts = store.list_facts(learner, kind="habit")
    assert len(facts) == 1
    assert facts[0]["key"] == "prefers_evening"
    assert facts[0]["value"]["value"] is False


def test_normalize_helpers():
    assert normalize_topic("OS.MEMORY") == "os.memory"
    assert normalize_topic("bogus") is None
    assert normalize_verdict("不会") == "unknown"
    assert normalize_verdict("weak") == "weak"
    assert parse_focus_topics("os.memory, cn.tcp, bogus") == ["os.memory", "cn.tcp"]

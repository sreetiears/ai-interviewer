"""考生长期记忆与个人知识库（SQLite）。

按 ``learner_id`` 持久化知识点掌握度、场次成绩单、已问历史，并为未来的
习惯/偏好类事实预留 ``kb_facts`` 表。Agent 与 HTTP 服务共用同一库文件。
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from config import config

logger = logging.getLogger("learner-store")

PRO_TOPICS: tuple[str, ...] = (
    "ds.structure",
    "ds.algo",
    "os.schedule",
    "os.memory",
    "os.fs",
    "cn.tcp",
    "cn.http",
    "cn.routing",
    "se.design",
    "se.tradeoff",
)

SOFT_TOPICS: tuple[str, ...] = (
    "soft.motive",
    "soft.plan",
    "soft.research",
    "soft.team",
    "soft.stress",
)

ALL_TOPICS: frozenset[str] = frozenset(PRO_TOPICS + SOFT_TOPICS)

TOPIC_LABELS: dict[str, str] = {
    "ds.structure": "数据结构",
    "ds.algo": "算法",
    "os.schedule": "操作系统·调度",
    "os.memory": "操作系统·内存",
    "os.fs": "操作系统·文件系统",
    "cn.tcp": "计算机网络·TCP",
    "cn.http": "计算机网络·HTTP",
    "cn.routing": "计算机网络·路由",
    "se.design": "系统设计",
    "se.tradeoff": "系统设计·取舍",
    "soft.motive": "读研动机",
    "soft.plan": "职业规划",
    "soft.research": "科研/竞赛经历",
    "soft.team": "团队合作",
    "soft.stress": "抗压",
}

VERDICT_DELTAS: dict[str, float] = {
    "strong": 0.15,
    "ok": 0.05,
    "weak": -0.2,
    "unknown": -0.3,
}

UNKNOWN_MASTERY = 0.5
FOCUS_TOPIC_COUNT = 3
_ASKED_HISTORY_LIMIT = 40

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learners (
    learner_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    report_json TEXT NOT NULL,
    weak_topics_json TEXT NOT NULL,
    completed_at REAL NOT NULL,
    FOREIGN KEY (learner_id) REFERENCES learners(learner_id)
);

CREATE TABLE IF NOT EXISTS topic_mastery (
    learner_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    mastery REAL NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL,
    PRIMARY KEY (learner_id, topic),
    FOREIGN KEY (learner_id) REFERENCES learners(learner_id)
);

CREATE TABLE IF NOT EXISTS asked_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id TEXT NOT NULL,
    question_norm TEXT NOT NULL,
    topic TEXT NOT NULL,
    asked_at REAL NOT NULL,
    FOREIGN KEY (learner_id) REFERENCES learners(learner_id)
);

CREATE INDEX IF NOT EXISTS idx_asked_learner_time
    ON asked_history (learner_id, asked_at DESC);

CREATE TABLE IF NOT EXISTS kb_facts (
    learner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (learner_id, kind, key),
    FOREIGN KEY (learner_id) REFERENCES learners(learner_id)
);
"""


def normalize_topic(raw: str | None) -> str | None:
    """把自由文本收成合法 topic；无法识别时返回 None。"""
    if not raw:
        return None
    topic = str(raw).strip().lower()
    if topic in ALL_TOPICS:
        return topic
    # 允许模型偶尔输出中文标签时做一次粗映射
    for key, label in TOPIC_LABELS.items():
        if topic == label.lower() or topic == label:
            return key
    return None


def normalize_verdict(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if text in VERDICT_DELTAS:
        return text
    if "不会" in text or "unknown" in text:
        return "unknown"
    if "强" in text or "好" in text or "strong" in text:
        return "strong"
    if "弱" in text or "不足" in text or "weak" in text:
        return "weak"
    return "ok"


def normalize_question(text: str) -> str:
    cleaned = re.sub(r"\s+", "", str(text or "").strip().lower())
    return cleaned[:200]


def clamp_mastery(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def decayed_mastery(
    mastery: float,
    last_updated: float,
    *,
    now: float | None = None,
    half_life_days: float | None = None,
) -> float:
    """按半衰期衰减掌握度；半衰期内减半。"""
    half_life = float(
        half_life_days if half_life_days is not None else config.mastery_half_life_days
    )
    if half_life <= 0:
        return clamp_mastery(mastery)
    current = time.time() if now is None else now
    days = max(0.0, (current - float(last_updated)) / 86400.0)
    factor = math.pow(0.5, days / half_life)
    return clamp_mastery(float(mastery) * factor)


def parse_focus_topics(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,，\s]+", str(raw).strip())
    out: list[str] = []
    for part in parts:
        topic = normalize_topic(part)
        if topic and topic not in out:
            out.append(topic)
    return out


@dataclass(frozen=True)
class TopicScore:
    topic: str
    mastery: float
    effective: float
    evidence_count: int
    last_updated: float
    label: str


@dataclass(frozen=True)
class ProfileSnapshot:
    learner_id: str
    focus_topics: list[str]
    weak_topics: list[TopicScore]
    recent_asked: list[str]
    recent_weakness: str
    session_count: int

    def prompt_text(self) -> str:
        """序列化成出题提示词可用的短摘要。"""
        lines: list[str] = []
        if self.focus_topics:
            labels = [TOPIC_LABELS.get(t, t) for t in self.focus_topics]
            lines.append("本场加强方向：" + "、".join(labels))
        if self.weak_topics:
            weak = [
                f"{item.label}({item.effective:.2f})"
                for item in self.weak_topics[:FOCUS_TOPIC_COUNT]
            ]
            lines.append("历史薄弱点（有效掌握度越低越弱）：" + "、".join(weak))
        if self.recent_asked:
            lines.append("跨场已问过（不要重复）：")
            for q in self.recent_asked[:12]:
                lines.append(f"- {q}")
        if self.recent_weakness:
            lines.append(f"最近一次核心不足：{self.recent_weakness}")
        return "\n".join(lines).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "learnerId": self.learner_id,
            "focusTopics": list(self.focus_topics),
            "weakTopics": [
                {
                    "topic": item.topic,
                    "label": item.label,
                    "mastery": round(item.mastery, 3),
                    "effective": round(item.effective, 3),
                    "evidenceCount": item.evidence_count,
                    "lastUpdated": item.last_updated,
                }
                for item in self.weak_topics
            ],
            "recentAsked": list(self.recent_asked),
            "recentWeakness": self.recent_weakness,
            "sessionCount": self.session_count,
        }


class LearnerStore:
    """线程安全的 SQLite 考生档案。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or config.learner_db_path)
        self._lock = threading.RLock()
        self._ensure_parent()
        self._init_schema()

    def _ensure_parent(self) -> None:
        if str(self.db_path) in {":memory:", ""}:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                conn.commit()

    def ensure_learner(self, learner_id: str) -> None:
        learner_id = str(learner_id or "").strip()
        if not learner_id:
            raise ValueError("learner_id is required")
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT learner_id FROM learners WHERE learner_id = ?",
                    (learner_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO learners (learner_id, created_at, last_seen_at) "
                        "VALUES (?, ?, ?)",
                        (learner_id, now, now),
                    )
                else:
                    conn.execute(
                        "UPDATE learners SET last_seen_at = ? WHERE learner_id = ?",
                        (now, learner_id),
                    )
                conn.commit()

    def get_topic_scores(self, learner_id: str) -> list[TopicScore]:
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT topic, mastery, evidence_count, last_updated "
                    "FROM topic_mastery WHERE learner_id = ?",
                    (learner_id,),
                ).fetchall()
        scores: list[TopicScore] = []
        for row in rows:
            mastery = float(row["mastery"])
            last_updated = float(row["last_updated"])
            scores.append(
                TopicScore(
                    topic=row["topic"],
                    mastery=mastery,
                    effective=decayed_mastery(mastery, last_updated, now=now),
                    evidence_count=int(row["evidence_count"] or 0),
                    last_updated=last_updated,
                    label=TOPIC_LABELS.get(row["topic"], row["topic"]),
                )
            )
        scores.sort(key=lambda item: (item.effective, item.topic))
        return scores

    def suggest_focus_topics(
        self,
        learner_id: str,
        *,
        explicit: Iterable[str] | None = None,
        limit: int = FOCUS_TOPIC_COUNT,
    ) -> list[str]:
        explicit_list = [t for t in (normalize_topic(x) for x in (explicit or [])) if t]
        if explicit_list:
            return explicit_list[:limit]
        scores = self.get_topic_scores(learner_id)
        weak = [s.topic for s in scores if s.effective < UNKNOWN_MASTERY]
        if not weak:
            weak = [s.topic for s in scores]
        return weak[:limit]

    def list_recent_asked(self, learner_id: str, *, limit: int = 20) -> list[str]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT question_norm FROM asked_history "
                    "WHERE learner_id = ? ORDER BY asked_at DESC LIMIT ?",
                    (learner_id, int(limit)),
                ).fetchall()
        # question_norm 是压缩文本；前端/prompt 里用原文更好，这里存的就是原文截断版
        return [str(row["question_norm"]) for row in rows]

    def list_sessions(self, learner_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT session_id, mode, report_json, weak_topics_json, completed_at "
                    "FROM sessions WHERE learner_id = ? "
                    "ORDER BY completed_at DESC LIMIT ?",
                    (learner_id, int(limit)),
                ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                report = json.loads(row["report_json"] or "{}")
            except (TypeError, ValueError):
                report = {}
            try:
                weak_topics = json.loads(row["weak_topics_json"] or "[]")
            except (TypeError, ValueError):
                weak_topics = []
            out.append(
                {
                    "sessionId": row["session_id"],
                    "mode": row["mode"],
                    "completedAt": row["completed_at"],
                    "totalScore": report.get("total_score"),
                    "coreWeakness": report.get("core_weakness") or "",
                    "weakTopics": weak_topics if isinstance(weak_topics, list) else [],
                }
            )
        return out

    def build_snapshot(
        self,
        learner_id: str,
        *,
        focus_topics: Iterable[str] | None = None,
    ) -> ProfileSnapshot:
        self.ensure_learner(learner_id)
        explicit = list(focus_topics or [])
        focus = self.suggest_focus_topics(learner_id, explicit=explicit)
        scores = self.get_topic_scores(learner_id)
        weak = [s for s in scores if s.effective < UNKNOWN_MASTERY][:FOCUS_TOPIC_COUNT]
        if not weak:
            weak = scores[:FOCUS_TOPIC_COUNT]
        sessions = self.list_sessions(learner_id, limit=1)
        recent_weakness = ""
        if sessions:
            recent_weakness = str(sessions[0].get("coreWeakness") or "")
        return ProfileSnapshot(
            learner_id=learner_id,
            focus_topics=focus,
            weak_topics=weak,
            recent_asked=self.list_recent_asked(learner_id, limit=12),
            recent_weakness=recent_weakness,
            session_count=len(self.list_sessions(learner_id, limit=100)),
        )

    def apply_verdict(
        self,
        learner_id: str,
        topic: str,
        verdict: str,
        *,
        now: float | None = None,
    ) -> float:
        topic_key = normalize_topic(topic)
        if not topic_key:
            raise ValueError(f"unknown topic: {topic!r}")
        verdict_key = normalize_verdict(verdict)
        delta = VERDICT_DELTAS[verdict_key]
        current = time.time() if now is None else now
        self.ensure_learner(learner_id)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT mastery, evidence_count FROM topic_mastery "
                    "WHERE learner_id = ? AND topic = ?",
                    (learner_id, topic_key),
                ).fetchone()
                if row is None:
                    mastery = clamp_mastery(UNKNOWN_MASTERY + delta)
                    evidence = 1
                    conn.execute(
                        "INSERT INTO topic_mastery "
                        "(learner_id, topic, mastery, evidence_count, last_updated) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (learner_id, topic_key, mastery, evidence, current),
                    )
                else:
                    mastery = clamp_mastery(float(row["mastery"]) + delta)
                    evidence = int(row["evidence_count"] or 0) + 1
                    conn.execute(
                        "UPDATE topic_mastery "
                        "SET mastery = ?, evidence_count = ?, last_updated = ? "
                        "WHERE learner_id = ? AND topic = ?",
                        (mastery, evidence, current, learner_id, topic_key),
                    )
                conn.commit()
        return mastery

    def remember_questions(
        self,
        learner_id: str,
        items: Iterable[tuple[str, str]],
        *,
        now: float | None = None,
    ) -> None:
        """写入已问历史。items 为 (question_text, topic) 列表。"""
        current = time.time() if now is None else now
        rows = []
        for question, topic in items:
            norm = normalize_question(question)
            topic_key = normalize_topic(topic) or "se.design"
            if not norm:
                continue
            rows.append((learner_id, norm, topic_key, current))
        if not rows:
            return
        self.ensure_learner(learner_id)
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO asked_history (learner_id, question_norm, topic, asked_at) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                # 裁剪过长历史，避免无限增长
                conn.execute(
                    "DELETE FROM asked_history WHERE learner_id = ? AND id NOT IN ("
                    "  SELECT id FROM asked_history WHERE learner_id = ? "
                    "  ORDER BY asked_at DESC LIMIT ?"
                    ")",
                    (learner_id, learner_id, _ASKED_HISTORY_LIMIT),
                )
                conn.commit()

    def record_session(
        self,
        *,
        learner_id: str,
        session_id: str,
        mode: str,
        report: dict[str, Any],
        turn_records: Iterable[dict[str, Any]] | None = None,
        asked_items: Iterable[tuple[str, str]] | None = None,
    ) -> list[str]:
        """落库一场面试，并按 turn_records 更新掌握度。返回 weak_topics。"""
        learner_id = str(learner_id or "").strip()
        session_id = str(session_id or "").strip()
        if not learner_id or not session_id:
            raise ValueError("learner_id and session_id are required")

        self.ensure_learner(learner_id)
        weak_topics: list[str] = []
        records = list(turn_records or [])
        for record in records:
            topic = normalize_topic(record.get("topic"))
            verdict = normalize_verdict(record.get("verdict"))
            if not topic:
                continue
            self.apply_verdict(learner_id, topic, verdict)
            if verdict in {"weak", "unknown"} and topic not in weak_topics:
                weak_topics.append(topic)

        report_weak = report.get("weak_topics") if isinstance(report, dict) else None
        if isinstance(report_weak, list):
            for item in report_weak:
                topic = normalize_topic(item)
                if topic and topic not in weak_topics:
                    weak_topics.append(topic)

        if asked_items:
            self.remember_questions(learner_id, asked_items)

        payload = dict(report) if isinstance(report, dict) else {}
        payload["weak_topics"] = weak_topics
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions "
                    "(session_id, learner_id, mode, report_json, weak_topics_json, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        learner_id,
                        str(mode or "quick"),
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(weak_topics, ensure_ascii=False),
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE learners SET last_seen_at = ? WHERE learner_id = ?",
                    (now, learner_id),
                )
                conn.commit()
        return weak_topics

    def upsert_fact(
        self,
        learner_id: str,
        kind: str,
        key: str,
        value: Any,
    ) -> None:
        kind = str(kind or "").strip() or "note"
        key = str(key or "").strip()
        if not key:
            raise ValueError("fact key is required")
        self.ensure_learner(learner_id)
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO kb_facts (learner_id, kind, key, value_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(learner_id, kind, key) DO UPDATE SET "
                    "value_json = excluded.value_json, updated_at = excluded.updated_at",
                    (
                        learner_id,
                        kind,
                        key,
                        json.dumps(value, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()

    def list_facts(
        self,
        learner_id: str,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                if kind:
                    rows = conn.execute(
                        "SELECT kind, key, value_json, updated_at FROM kb_facts "
                        "WHERE learner_id = ? AND kind = ? ORDER BY updated_at DESC",
                        (learner_id, kind),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT kind, key, value_json, updated_at FROM kb_facts "
                        "WHERE learner_id = ? ORDER BY updated_at DESC",
                        (learner_id,),
                    ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["value_json"])
            except (TypeError, ValueError):
                value = row["value_json"]
            out.append(
                {
                    "kind": row["kind"],
                    "key": row["key"],
                    "value": value,
                    "updatedAt": row["updated_at"],
                }
            )
        return out


_default_store: LearnerStore | None = None
_default_lock = threading.Lock()


def get_store(db_path: str | Path | None = None) -> LearnerStore:
    """返回进程内默认 store；测试可传入独立 db_path。"""
    if db_path is not None:
        return LearnerStore(db_path)
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = LearnerStore()
        return _default_store


def reset_default_store_for_tests() -> None:
    """仅供测试重置全局单例。"""
    global _default_store
    with _default_lock:
        _default_store = None

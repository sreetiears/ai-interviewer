"""把 Markdown 讲义切成带 topic / source 元数据的块。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeChunk:
    """入库前的一块文本。"""

    chunk_id: str
    text: str
    topic: str
    source: str


_TOPIC_FROM_NAME = re.compile(r"^([a-z]+\.[a-z0-9_]+)")
_HEADING = re.compile(r"(?m)^(#{1,3})\s+(.+)$")


def topic_from_path(path: Path) -> str | None:
    """``os.memory.md`` / ``os.memory-paging.md`` → ``os.memory``。"""
    match = _TOPIC_FROM_NAME.match(path.stem)
    return match.group(1) if match else None


def split_markdown(text: str) -> list[str]:
    """按标题切开；没有标题时按空行分段。"""
    text = text.strip()
    if not text:
        return []
    matches = list(_HEADING.finditer(text))
    if not matches:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        return parts or [text]

    chunks: list[str] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        chunks.append(preamble)
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            chunks.append(block)
    return chunks


def load_knowledge_dir(knowledge_dir: str | Path) -> list[KnowledgeChunk]:
    """扫描目录下 ``*.md``，按文件名解析 topic 并切块。"""
    root = Path(knowledge_dir)
    if not root.is_dir():
        return []

    out: list[KnowledgeChunk] = []
    for path in sorted(root.glob("*.md")):
        topic = topic_from_path(path)
        if not topic:
            continue
        raw = path.read_text(encoding="utf-8")
        parts = split_markdown(raw)
        source = path.name
        for index, part in enumerate(parts):
            chunk_id = f"{path.stem}::{index}"
            out.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    text=part,
                    topic=topic,
                    source=source,
                )
            )
    return out

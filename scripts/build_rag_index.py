"""把 ``knowledge/`` 讲义写入 Chroma 持久化索引。

用法（在 ``ai-interviewer`` 根目录）::

    python scripts/build_rag_index.py
    python scripts/build_rag_index.py --fake   # 无 API Key 时用确定性伪向量

默认走 SiliconFlow / OpenAI-compatible embedding（见 ``EMBEDDING_MODEL``）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import config  # noqa: E402
from rag.embeddings import FakeEmbedding, default_embedder_from_config  # noqa: E402
from rag.index import KnowledgeIndex  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build interview RAG index from knowledge/")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Use FakeEmbedding instead of remote API (for offline/dev).",
    )
    parser.add_argument(
        "--knowledge-dir",
        default=config.rag_knowledge_dir,
        help="Markdown knowledge directory",
    )
    parser.add_argument(
        "--persist-path",
        default=config.rag_persist_path,
        help="Chroma persist directory",
    )
    args = parser.parse_args()

    embedder = FakeEmbedding() if args.fake else default_embedder_from_config()
    index = KnowledgeIndex(persist_path=args.persist_path, embedder=embedder)
    count = index.ingest_directory(args.knowledge_dir, replace=True)
    print(f"Indexed {count} chunks → {args.persist_path} (collection count={index.count})")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())

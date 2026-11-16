"""双文档匹配 RAG：薄弱点档案（查询侧）× 专业课知识库（语料侧）。"""

from rag.retrieve import (
    build_query,
    format_passages_for_prompt,
    match_documents,
    retrieve_for_interview,
)

__all__ = [
    "build_query",
    "format_passages_for_prompt",
    "match_documents",
    "retrieve_for_interview",
]

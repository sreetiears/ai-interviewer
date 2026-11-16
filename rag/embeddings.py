"""OpenAI-compatible embedding，以及测试用确定性伪向量。"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol, Sequence

logger = logging.getLogger("rag-embeddings")

# 与 learner_store.PRO_TOPICS 对齐，供 FakeEmbedding 做可测的主题偏置。
_TOPIC_TOKENS: dict[str, tuple[str, ...]] = {
    "ds.structure": ("数据结构", "链表", "树", "图", "栈", "队列", "哈希"),
    "ds.algo": ("算法", "复杂度", "排序", "动态规划", "贪心", "搜索"),
    "os.schedule": ("调度", "进程", "线程", "时间片", "优先级", "死锁"),
    "os.memory": ("内存", "分页", "虚拟内存", "缺页", "页表", "TLB", "置换"),
    "os.fs": ("文件系统", "inode", "目录", "磁盘", "缓冲"),
    "cn.tcp": ("TCP", "三次握手", "四次挥手", "拥塞", "滑动窗口", "可靠"),
    "cn.http": ("HTTP", "HTTPS", "REST", "状态码", "Cookie", "缓存"),
    "cn.routing": ("路由", "OSPF", "BGP", "IP", "转发", "网关"),
    "se.design": ("系统设计", "架构", "高并发", "缓存", "消息队列", "微服务"),
    "se.tradeoff": ("取舍", "一致性", "可用性", "CAP", "权衡", "延迟"),
}


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...


class FakeEmbedding:
    """确定性伪向量：相同文本同向量；含主题词时在对应维度有偏置。

    离线测试不依赖真实 embedding API。维度固定为 64。
    """

    DIM = 64

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(self.DIM)]
        lowered = text.lower()
        for topic_index, (topic, tokens) in enumerate(_TOPIC_TOKENS.items()):
            hit = topic in lowered or any(tok.lower() in lowered for tok in tokens)
            if hit:
                values[topic_index % self.DIM] += 3.0
        return _l2_normalize(values)


class OpenAICompatibleEmbedding:
    """走与 LLM 相同的 base_url / api_key（如 SiliconFlow）。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 30.0,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # 部分兼容接口一次一批更稳；文本已切块，批量调用即可。
        response = self._client.embeddings.create(model=self._model, input=list(texts))
        by_index = {item.index: item.embedding for item in response.data}
        return [list(by_index[i]) for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []


def default_embedder_from_config() -> Embedder:
    """按当前 ``config`` 构造真实 embedder；缺 Key 时仍返回对象，调用时会失败。"""
    from config import config, resolve_api_key

    return OpenAICompatibleEmbedding(
        api_key=resolve_api_key(config.llm_api_key or config.siliconflow_api_key),
        base_url=config.embedding_base_url,
        model=config.embedding_model,
        timeout=config.llm_timeout_seconds,
    )


def _l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def tokenize_for_debug(text: str) -> list[str]:
    """测试辅助：粗分词。"""
    return [t for t in re.split(r"\W+", text) if t]

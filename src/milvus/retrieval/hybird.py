"""混合检索：BM25 + 向量 双路召回 → RRF 融合 → Rerank 精排。

对应 CLAUDE.md §7.3 全流程：
    BM25 top 20 ──┐
                  ├── RRF 融合 top 30 ── Rerank ── top 5 ──▶ 拼进 Prompt
    向量 top 20 ──┘

RRF（Reciprocal Rank Fusion）只看名次不看原始分：
    score(d) = Σ 1/(k + rank_i(d))
天然规避了 BM25 分数与余弦相似度不同量纲、无法直接加权的问题。
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

from .bm25 import BM25Retriever
from .reranker import Reranker
from .retriever import VectorRetriever

logger = logging.getLogger(__name__)

# RRF 平滑常数，60 是原论文默认值
RRF_K = 60


def chunk_key(doc: dict) -> tuple:
    """块的跨检索器身份标识：(knowledge_id, 内容 md5)。

    Milvus 没存块级 id，两路结果只能靠「所属文档 + 内容指纹」对齐。
    """
    md5 = hashlib.md5(doc["content"].encode("utf-8")).hexdigest()
    return (doc["knowledge_id"], md5)


def rrf_fuse(result_lists: list[list[dict]], k: int = RRF_K) -> list[dict]:
    """RRF 融合多路检索结果，按融合分降序返回。

    :param result_lists: 多路结果，每路内部已按各自分数降序
    :return: [{...doc, "rrf_score": float, "matched_by": [...]}, ...]
    """
    fused: dict[tuple, dict] = {}

    for docs in result_lists:
        for rank, doc in enumerate(docs, start=1):
            key = chunk_key(doc)
            entry = fused.get(key)
            if entry is None:
                entry = {**doc, "rrf_score": 0.0, "matched_by": []}
                fused[key] = entry
            entry["rrf_score"] += 1.0 / (k + rank)
            src = doc.get("source")
            if src and src not in entry["matched_by"]:
                entry["matched_by"].append(src)

    return sorted(fused.values(), key=lambda d: d["rrf_score"], reverse=True)


class HybridRetriever:
    """混合检索编排：两路召回 → RRF → Rerank"""

    def __init__(
        self,
        db: Session,
        bm25_top: int = 20,
        vector_top: int = 20,
        fuse_top: int = 30,
    ):
        self.bm25 = BM25Retriever(db)
        self.vector = VectorRetriever()
        self.reranker = Reranker()
        self.bm25_top = bm25_top
        self.vector_top = vector_top
        self.fuse_top = fuse_top

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        uid: int = 0,
    ) -> list[dict]:
        """完整检索链路，返回精排后的最终结果

        :param uid: 请求者用户；个人知识仅本人可检索（全局块所有人可见）
        :return: [{knowledge_id, content, category, score, rrf_score, matched_by, source}, ...]
        """
        # 1) 双路召回（向量路不可用时降级为单路，不让整个对话挂掉）
        bm25_hits = self.bm25.search(
            query, top_k=self.bm25_top, category=category, uid=uid
        )
        try:
            vec_hits = self.vector.search(
                query, top_k=self.vector_top, category=category, uid=uid
            )
        except Exception as e:  # noqa: BLE001 —— Milvus/Milvus Lite 不可用时保底
            logger.warning("[retrieval] 向量路不可用，降级为纯 BM25：%s", e)
            vec_hits = []

        # 2) RRF 融合，截断到 fuse_top
        fused = rrf_fuse([bm25_hits, vec_hits])[: self.fuse_top]
        if not fused:
            return []

        # 3) Rerank 精排，以重排分作为最终 score
        try:
            reranked = self.reranker.rerank(
                query, [d["content"] for d in fused], top_k=top_k
            )
        except Exception as e:  # noqa: BLE001 —— rerank API 挂了也不阻断对话
            logger.warning("[retrieval] Rerank 不可用，按 RRF 顺序返回：%s", e)
            return [{**doc, "score": doc["rrf_score"]} for doc in fused[:top_k]]
        results = []
        for r in reranked:
            doc = fused[r["index"]]
            results.append(
                {**doc, "score": r["relevance_score"], "rerank_score": r["relevance_score"]}
            )
        return results

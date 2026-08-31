"""BM25 关键词检索：jieba 分词 + rank_bm25，语料缓存落盘。

对应 CLAUDE.md §7.3 左支路：query → jieba 分词 → BM25 → top 20。

设计要点：
- 语料粒度是「块」，与向量化流水线同一套切块参数，
  这样 RRF 融合时两路结果能按块对齐
- 语料缓存落盘到 DATA_DIR/bm25_cache.pkl；
  缓存签名 =（已向量化篇数, updated_at 最大值），知识库一变自动重建
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import settings
from milvus.ingestion.spliter import split_knowledge
from model.KnowledgeModel import KnowledgeModel

logger = logging.getLogger(__name__)

# 缓存文件名
CACHE_NAME = "bm25_cache.pkl"


def tokenize(text: str) -> list[str]:
    """jieba 分词：去空白、英文转小写"""
    return [
        t.lower()
        for t in jieba.cut(text)
        if t.strip()
    ]


@dataclass
class CacheSignature:
    """缓存签名：已向量化知识数量 + updated_at 最大值"""

    count: int
    max_updated_at: float  # 时间戳，便于序列化比较


class BM25Retriever:
    """BM25 检索器，语料自动构建/落盘缓存"""

    def __init__(self, db: Session, cache_path: Path | None = None):
        self.db = db
        self.cache_path = cache_path or Path(settings.DATA_DIR) / CACHE_NAME
        self.docs: list[dict] = []  # [{"knowledge_id","content","category"}, ...]
        self.bm25: BM25Okapi | None = None
        self._ensure_ready()

    # ---------- 语料构建 ----------

    def _db_signature(self) -> CacheSignature:
        """从 MySQL 计算当前已向量化知识的签名"""
        count, max_updated = (
            self.db.query(
                func.count(KnowledgeModel.id),
                func.max(KnowledgeModel.updated_at),
            )
            .filter(KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED)
            .one()
        )
        ts = max_updated.timestamp() if isinstance(max_updated, datetime) else 0.0
        return CacheSignature(count=count or 0, max_updated_at=ts)

    def _ensure_ready(self) -> None:
        """缓存命中则加载，否则重建并落盘"""
        sig = self._db_signature()
        cached = self._load_cache()
        if cached and cached["signature"] == (sig.count, sig.max_updated_at):
            self.docs = cached["docs"]
            logger.info("[bm25] 缓存命中：%s 块", len(self.docs))
        else:
            self._rebuild(sig)
        if self.docs:
            self.bm25 = BM25Okapi([tokenize(d["content"]) for d in self.docs])

    def _load_cache(self) -> dict | None:
        """读落盘缓存；文件损坏视为未命中"""
        if not self.cache_path.exists():
            return None
        try:
            with open(self.cache_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:  # noqa: BLE001 —— 缓存坏了重建即可
            logger.warning("[bm25] 缓存读取失败，将重建：%s", e)
            return None

    def _rebuild(self, sig: CacheSignature) -> None:
        """全量重建：拉 status=1 知识 → 同一套切块 → 落盘"""
        rows = (
            self.db.query(KnowledgeModel)
            .filter(KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED)
            .order_by(KnowledgeModel.id)
            .all()
        )
        docs: list[dict] = []
        for row in rows:
            for chunk in split_knowledge(row):
                docs.append(
                    {
                        "knowledge_id": chunk.knowledge_id,
                        "content": chunk.text,
                        "category": chunk.category,
                    }
                )
        self.docs = docs

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "wb") as f:
            pickle.dump(
                {"signature": (sig.count, sig.max_updated_at), "docs": docs}, f
            )
        logger.info("[bm25] 缓存重建完成：%s 篇知识 → %s 块", len(rows), len(docs))

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        top_k: int = 20,
        category: str | None = None,
    ) -> list[dict]:
        """BM25 检索，返回按分数降序的块列表

        :return: [{"knowledge_id","content","category","score","source":"bm25"}, ...]
        """
        if not self.bm25:
            return []

        scores = self.bm25.get_scores(tokenize(query))

        # category 过滤：不符合的分数置 -1，排到最后自然被截掉
        if category:
            scores = [
                s if d["category"] == category else -1.0
                for s, d in zip(scores, self.docs)
            ]

        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                break  # BM25 分数为 0 说明没有任何词命中，后面更不用看
            d = self.docs[i]
            results.append(
                {
                    "knowledge_id": d["knowledge_id"],
                    "content": d["content"],
                    "category": d["category"],
                    "score": float(scores[i]),
                    "source": "bm25",
                }
            )
        return results

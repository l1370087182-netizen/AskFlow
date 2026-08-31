"""入库流水线：loader → spliter → embeddings → VectorStore → 回写 status。

流程（对应 CLAUDE.md §7.2）：
    1. 扫 status=0 的知识
    2. 逐篇：切块 → 向量化 → 写 Milvus
    3. 成功回写 status=1；任何一步抛异常回写 status=2，不影响其他文档

幂等性：写入前先按 knowledge_id 清旧块，
所以文档内容更新（upsert 重置 status=0）后重跑不会产生重复块。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from model.KnowledgeModel import KnowledgeModel

from .embeddings import EmbeddingClient
from .loader import KnowledgeLoader
from .spliter import split_knowledge
from .VectorStore import VectorStore

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """向量化入库流水线"""

    def __init__(
        self,
        db: Session,
        vector_store: VectorStore | None = None,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.loader = KnowledgeLoader(db)
        self.dao = self.loader.dao  # 复用同一个 DAO 回写状态，共享 Session
        self.store = vector_store or VectorStore()
        self.embedder = embedding_client or EmbeddingClient()

    def run(self, limit: int | None = None) -> dict:
        """跑完一轮流水线，返回统计信息

        :param limit: 只处理前 N 条待向量化知识，None 表示全部
        :return: {"total", "success", "failed", "chunks"}
        """
        rows = self.loader.load_pending(limit=limit)
        stats = {"total": len(rows), "success": 0, "failed": 0, "chunks": 0}
        logger.info("[ingestion] 本轮待处理 %s 篇", stats["total"])

        for row in rows:
            try:
                n = self._ingest_one(row)
                self.dao.update_status(row.id, KnowledgeModel.STATUS_EMBEDDED)
                stats["success"] += 1
                stats["chunks"] += n
                logger.info(
                    "[ingestion] id=%s《%s》切 %s 块，向量化入库成功",
                    row.id, row.title, n,
                )
            except Exception as e:  # noqa: BLE001 —— 单篇失败不中断整轮
                self.dao.update_status(row.id, KnowledgeModel.STATUS_FAILED)
                stats["failed"] += 1
                logger.exception(
                    "[ingestion] id=%s《%s》向量化失败：%s", row.id, row.title, e
                )

        logger.info("[ingestion] 本轮完成：%s", stats)
        return stats

    def _ingest_one(self, row: KnowledgeModel) -> int:
        """单篇文档：清旧块 → 切块 → 向量化 → 写 Milvus，返回写入块数"""
        # 先清旧块（重跑/内容更新场景），保证幂等
        self.store.delete_by_knowledge(row.id)

        chunks = split_knowledge(row)
        if not chunks:
            logger.warning("[ingestion] id=%s《%s》内容为空，无块可写", row.id, row.title)
            return 0

        vectors = self.embedder.embed_texts([c.text for c in chunks])
        return self.store.insert_chunks(chunks, vectors)

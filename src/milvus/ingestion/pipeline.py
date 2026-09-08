"""入库流水线：loader → spliter → embeddings → VectorStore → 回写 status。

流程（对应 CLAUDE.md §7.2）：
    1. 扫 status=0 的知识
    2. 逐篇：切块 → 向量化 → 写 Milvus
    3. 成功回写 status=1；任何一步抛异常回写 status=2，不影响其他文档

幂等性：写入前先按 knowledge_id 清旧块，
所以文档内容更新（upsert 重置 status=0）后重跑不会产生重复块。

并发安全（个人知识库）：个人条目走 ingest_row 即时入库，与批量流水线
可能同时发生。「清旧块→写新块」这对操作必须原子，否则会出现
A 清完旧块、B 也清/写、交错出孤儿块。用进程级写锁串行化该窗口；
批量流水线与即时入库共享同一把锁。
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from core.config import settings
from model.KnowledgeModel import KnowledgeModel

from .embeddings import EmbeddingClient
from .loader import KnowledgeLoader
from .spliter import split_knowledge
from .VectorStore import VectorStore, get_vector_store

logger = logging.getLogger(__name__)

# 进程级写锁：串行化「清旧块→写新块」窗口（批量流水线 + 即时入库共用）
_ingest_lock = threading.Lock()


def _err_summary(e: Exception, limit: int = 500) -> str:
    """异常 → 可落库的原因摘要（去换行防串行，截断到列宽内）"""
    return f"{type(e).__name__}: {e}".replace("\n", " ")[:limit]


def _needs_graph(row: KnowledgeModel) -> bool:
    """该条内容是否需要【流水线】建图（避免与 reviewer 质检搭车建图重复）。

    - 个人爬取（source_type=personal 且真实 URL）：producer 向量化的【同时】reviewer
      质检会搭车抽实体/关系建图（零额外成本），流水线跳过，否则同一篇建两遍灌 weight。
    - 手工条目（personal + source_url=manual://…）、上传（upload）、全局爬虫（spider）：
      质检【不覆盖】（手工/上传豁免；全局爬虫不走 Agent 引擎），由流水线建图。
    """
    st = (row.source_type or "").lower()
    url = row.source_url or ""
    if st == "personal" and not url.startswith("manual://"):
        return False  # 个人爬取 → reviewer 建图，流水线不重复
    return True


class IngestionPipeline:
    """向量化入库流水线"""

    def __init__(
        self,
        db: Session,
        vector_store: VectorStore | None = None,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.db = db  # 建图时取属主模型 / 写 kg_node·kg_edge 用
        self.loader = KnowledgeLoader(db)
        self.dao = self.loader.dao  # 复用同一个 DAO 回写状态，共享 Session
        # 默认复用进程级单例：Milvus Lite 单进程独占，避免重复建连接
        self.store = vector_store or get_vector_store()
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
                self.dao.update_status(row.id, KnowledgeModel.STATUS_EMBEDDED)  # error 默认清除
                stats["success"] += 1
                stats["chunks"] += n
                logger.info(
                    "[ingestion] id=%s《%s》切 %s 块，向量化入库成功",
                    row.id, row.title, n,
                )
                self._maybe_build_graph(row)  # 向量化成功后顺带建图（可降级，不影响入库）
            except Exception as e:  # noqa: BLE001 —— 单篇失败不中断整轮
                self.dao.update_status(
                    row.id, KnowledgeModel.STATUS_FAILED, error=_err_summary(e)
                )
                stats["failed"] += 1
                logger.exception(
                    "[ingestion] id=%s《%s》向量化失败：%s", row.id, row.title, e
                )

        logger.info("[ingestion] 本轮完成：%s", stats)
        return stats

    def ingest_row(self, row: KnowledgeModel) -> int:
        """单条即时入库（个人知识手工添加/爬取/编辑后用），返回写入块数。

        成功回写 status=1；任何一步异常回写 status=2 并上抛，
        由调用方决定如何向前端呈现（条目已保存，但向量化失败）。
        """
        try:
            n = self._ingest_one(row)
            self.dao.update_status(row.id, KnowledgeModel.STATUS_EMBEDDED)
            logger.info(
                "[ingestion] id=%s《%s》即时入库 %s 块", row.id, row.title, n
            )
            self._maybe_build_graph(row)  # 向量化成功后顺带建图（可降级，不影响入库）
            return n
        except Exception as e:
            self.dao.update_status(
                row.id, KnowledgeModel.STATUS_FAILED, error=_err_summary(e)
            )
            logger.exception(
                "[ingestion] id=%s《%s》即时向量化失败", row.id, row.title
            )
            raise

    def _ingest_one(self, row: KnowledgeModel) -> int:
        """单篇文档：清旧块 → 切块 → 向量化 → 写 Milvus，返回写入块数

        「清旧块→写新块」整体在进程级写锁内，避免与并发入库交错。
        """
        with _ingest_lock:
            # 先清旧块（重跑/内容更新场景），保证幂等
            self.store.delete_by_knowledge(row.id)

            chunks = split_knowledge(row)
            if not chunks:
                logger.warning("[ingestion] id=%s《%s》内容为空，无块可写", row.id, row.title)
                return 0

            vectors = self.embedder.embed_texts([c.text for c in chunks])
            return self.store.insert_chunks(chunks, vectors)

    def _maybe_build_graph(self, row: KnowledgeModel) -> None:
        """向量化成功后顺带建知识图谱（实体节点 + 共现边 + 关系边）。

        只处理 reviewer 质检【不覆盖】的内容（见 _needs_graph）：手工/上传/全局爬虫。
        个人爬取由 reviewer 搭车建图，这里跳过避免重复灌 weight。

        全程可降级，绝不阻断入库：
        - 开关关 / 个人爬取 → 直接跳过
        - 无可用模型（全局内容 user_id=0、或属主未配置）→ 跳过并留痕，
          之后可由 scripts/backfill_kg.py 借模型补建
        - 抽取/写图任何异常 → 记日志跳过，向量化结果不受影响
        """
        if not settings.KG_BUILD_ON_INGEST or not _needs_graph(row):
            return
        try:
            from generation.llm import build_llm_for_user  # 延迟导入，避免模块级循环

            llm = build_llm_for_user(self.db, row.user_id)
            if llm is None:
                logger.info(
                    "[ingestion] id=%s 无可用模型（全局内容/属主未配置），跳过建图；"
                    "可由 scripts/backfill_kg.py 借模型补建", row.id,
                )
                return

            from agents.quality import extract_graph
            from DAO.kg_dao import KgDAO

            entities, triples = extract_graph(llm, row.title, row.category, row.content)
            if not (entities or triples):
                return
            n = KgDAO(self.db).learn_document(
                row.user_id, entities, row.category, triples
            )
            logger.info(
                "[ingestion] id=%s《%s》建图：%s 节点 / %s 关系",
                row.id, row.title, n, len(triples),
            )
        except Exception as e:  # noqa: BLE001 —— 建图是增强，故障绝不影响向量化
            logger.warning("[ingestion] id=%s 建图失败（跳过）：%s", row.id, e)

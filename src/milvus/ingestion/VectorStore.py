"""Milvus 集合封装：建 collection、insert、search、count、delete。

collection 结构（对应 CLAUDE.md §7.2 + 个人知识库扩展）：
    id           INT64        主键（auto_id）
    vector       FLOAT_VECTOR bge-m3 向量（1024 维）
    knowledge_id INT64        来源知识主键（MySQL knowledge.id）
    content      VARCHAR      块文本
    category     VARCHAR      技术分类（检索时可按类过滤）
    user_id      INT64        所属用户；0=全局知识（检索时按人隔离）
"""
from __future__ import annotations

import logging
import threading

from pymilvus import DataType, MilvusClient

from core.config import settings

from .spliter import Chunk

logger = logging.getLogger(__name__)


class VectorStore:
    """knowledge_chunks 集合的增删查封装"""

    COLLECTION = "knowledge_chunks"
    DIM = 1024  # bge-m3 输出维度

    def __init__(self, host: str | None = None, port: int | None = None):
        if settings.MILVUS_LITE_PATH:
            # 嵌入式 Milvus Lite（单文件库），低内存服务器部署用；本地开发留空走 Standalone
            self.client = MilvusClient(uri=settings.MILVUS_LITE_PATH)
        else:
            host = host or settings.MILVUS_HOST
            port = port or settings.MILVUS_PORT
            # MilvusClient 是 pymilvus 的新式轻量 API，uri 走 HTTP 协议
            self.client = MilvusClient(uri=f"http://{host}:{port}")
        self._ensure_collection()

    # ---------- 集合管理 ----------

    def _collection_fields(self) -> set[str]:
        """现有集合的字段名集合；集合不存在返回空集"""
        desc = self.client.describe_collection(self.COLLECTION)
        return {f["name"] for f in desc.get("fields", [])}

    def _ensure_collection(self) -> None:
        """集合不存在则创建（含向量索引），并确保已 load 进内存。

        存量集合缺 user_id 字段（个人知识库之前的旧 schema）→ drop 重建：
        Milvus 不支持给已有集合安全地补主键/索引结构，直接重建最稳；
        MySQL 侧 status 已在迁移时重置为 0，重跑 /api/embedding/run 即可回灌。
        重建窗口期混合检索自动降级为纯 BM25，对话不整体失败。
        """
        if self.client.has_collection(self.COLLECTION):
            if "user_id" not in self._collection_fields():
                logger.warning(
                    "[vectorstore] 存量集合 %s 缺 user_id 字段，drop 重建；"
                    "请重跑 /api/embedding/run 回灌全部知识",
                    self.COLLECTION,
                )
                self.client.drop_collection(self.COLLECTION)
            else:
                self.client.load_collection(self.COLLECTION)
                return

        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True, description="块主键")
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.DIM, description="bge-m3 向量")
        schema.add_field("knowledge_id", DataType.INT64, description="来源知识主键（MySQL knowledge.id）")
        # 块最长约 500 字（中文 UTF-8 约 1500 字节），4096 足够宽裕
        schema.add_field("content", DataType.VARCHAR, max_length=4096, description="块文本")
        schema.add_field("category", DataType.VARCHAR, max_length=128, description="技术分类")
        schema.add_field("user_id", DataType.INT64, description="所属用户；0=全局知识")

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",  # 交给 Milvus 自选索引（单节点下一般是 HNSW）
            metric_type="COSINE",  # bge-m3 推荐余弦相似度
        )

        self.client.create_collection(
            collection_name=self.COLLECTION,
            schema=schema,
            index_params=index_params,
        )
        self.client.load_collection(self.COLLECTION)

    # ---------- 写入 ----------

    def insert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """块与向量一一对应写入，返回插入条数"""
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks({len(chunks)}) 与 vectors({len(vectors)}) 数量不一致")
        if not chunks:
            return 0
        rows = [
            {
                "knowledge_id": c.knowledge_id,
                "content": c.text,
                "category": c.category,
                "user_id": c.user_id,
                "vector": v,
            }
            for c, v in zip(chunks, vectors)
        ]
        res = self.client.insert(collection_name=self.COLLECTION, data=rows)
        return res["insert_count"]

    def delete_by_knowledge(self, knowledge_id: int) -> int:
        """删除某篇文档的全部块。

        文档重新入库（upsert 会把 status 重置为 0）后重跑流水线时，
        先清旧块再写新块，避免新旧块并存造成重复。
        """
        res = self.client.delete(
            collection_name=self.COLLECTION,
            filter=f"knowledge_id == {knowledge_id}",
        )
        return res.get("delete_count", 0)

    # ---------- 查询 ----------

    def count(self) -> int:
        """集合内总块数"""
        rows = self.client.query(
            collection_name=self.COLLECTION,
            filter="",
            output_fields=["count(*)"],
        )
        return rows[0]["count(*)"]

    def count_by_knowledge(self, knowledge_id: int) -> int:
        """单篇文档的块数（验证入库结果用）"""
        rows = self.client.query(
            collection_name=self.COLLECTION,
            filter=f"knowledge_id == {knowledge_id}",
            output_fields=["count(*)"],
        )
        return rows[0]["count(*)"]

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        category: str | None = None,
        uid: int = 0,
    ) -> list[dict]:
        """向量检索（阶段 6 的 retriever.py 会用到，这里先备好）

        归属过滤（个人知识库）：uid=0 只看全局块（user_id == 0）；
        uid>0 看全局 + 本人块（user_id == 0 or user_id == uid）。
        与 category 条件用 and 拼接。

        :return: [{"score", "knowledge_id", "content", "category"}, ...] 按相似度降序
        """
        parts: list[str] = []
        if category:
            parts.append(f'category == "{category}"')
        if uid:
            parts.append(f"(user_id == 0 or user_id == {int(uid)})")
        else:
            parts.append("user_id == 0")

        res = self.client.search(
            collection_name=self.COLLECTION,
            data=[query_vector],
            limit=top_k,
            filter=" and ".join(parts),
            output_fields=["knowledge_id", "content", "category"],
        )
        return [
            {
                "score": hit["distance"],
                "knowledge_id": hit["entity"]["knowledge_id"],
                "content": hit["entity"]["content"],
                "category": hit["entity"]["category"],
            }
            for hit in res[0]
        ]


# ---------- 进程级单例 ----------

_store_instance: VectorStore | None = None
_store_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    """获取进程内唯一的 VectorStore（双重检查锁，线程安全）。

    两个原因不要每个请求都新建：
    1. Milvus Lite 是单进程独占的嵌入式库，客户端构造会拉起/绑定本地
       milvus 子进程，频繁新建容易造成连接抖动与锁残留；
    2. 构造时会做 has_collection / load_collection，每请求重复纯属浪费。
    全进程共享一个实例，Milvus Lite 子进程随后端进程同生命周期。
    """
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = VectorStore()
    return _store_instance

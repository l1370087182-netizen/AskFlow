"""Milvus 集合封装：建 collection、insert、search、count、delete。

collection 结构（对应 CLAUDE.md §7.2）：
    id           INT64        主键（auto_id）
    vector       FLOAT_VECTOR bge-m3 向量（1024 维）
    knowledge_id INT64        来源知识主键（MySQL knowledge.id）
    content      VARCHAR      块文本
    category     VARCHAR      技术分类（检索时可按类过滤）
"""
from __future__ import annotations

from pymilvus import DataType, MilvusClient

from core.config import settings

from .spliter import Chunk


class VectorStore:
    """knowledge_chunks 集合的增删查封装"""

    COLLECTION = "knowledge_chunks"
    DIM = 1024  # bge-m3 输出维度

    def __init__(self, host: str | None = None, port: int | None = None):
        host = host or settings.MILVUS_HOST
        port = port or settings.MILVUS_PORT
        # MilvusClient 是 pymilvus 的新式轻量 API，uri 走 HTTP 协议
        self.client = MilvusClient(uri=f"http://{host}:{port}")
        self._ensure_collection()

    # ---------- 集合管理 ----------

    def _ensure_collection(self) -> None:
        """集合不存在则创建（含向量索引），并确保已 load 进内存"""
        if self.client.has_collection(self.COLLECTION):
            self.client.load_collection(self.COLLECTION)
            return

        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True, description="块主键")
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.DIM, description="bge-m3 向量")
        schema.add_field("knowledge_id", DataType.INT64, description="来源知识主键（MySQL knowledge.id）")
        # 块最长约 500 字（中文 UTF-8 约 1500 字节），4096 足够宽裕
        schema.add_field("content", DataType.VARCHAR, max_length=4096, description="块文本")
        schema.add_field("category", DataType.VARCHAR, max_length=128, description="技术分类")

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
    ) -> list[dict]:
        """向量检索（阶段 6 的 retriever.py 会用到，这里先备好）

        :return: [{"score", "knowledge_id", "content", "category"}, ...] 按相似度降序
        """
        res = self.client.search(
            collection_name=self.COLLECTION,
            data=[query_vector],
            limit=top_k,
            filter=f'category == "{category}"' if category else "",
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

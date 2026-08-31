"""向量检索：query → bge-m3 向量化 → Milvus ANN 检索。

对应 CLAUDE.md §7.3 右支路：query → 向量检索 → top 20。
复用阶段 5 的 EmbeddingClient 和 VectorStore。
"""
from __future__ import annotations

from milvus.ingestion.embeddings import EmbeddingClient
from milvus.ingestion.VectorStore import VectorStore


class VectorRetriever:
    """Milvus 向量检索器"""

    def __init__(
        self,
        store: VectorStore | None = None,
        embedder: EmbeddingClient | None = None,
    ):
        self.store = store or VectorStore()
        self.embedder = embedder or EmbeddingClient()

    def search(
        self,
        query: str,
        top_k: int = 20,
        category: str | None = None,
    ) -> list[dict]:
        """向量检索，返回按余弦相似度降序的块列表

        :return: [{"knowledge_id","content","category","score","source":"vector"}, ...]
        """
        qvec = self.embedder.embed_texts([query])[0]
        hits = self.store.search(qvec, top_k=top_k, category=category)
        return [{**h, "source": "vector"} for h in hits]

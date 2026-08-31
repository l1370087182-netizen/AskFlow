"""Rerank 精排：OpenAI 兼容的 /rerank 接口调 bge-reranker。

对应 CLAUDE.md §7.3 最后一步：融合后的 top 30 → 精排 → top 5。
SiliconFlow 约定：
    POST {base}/rerank
    {"model": "...", "query": "...", "documents": [...], "top_n": N}
    返回 {"results": [{"index": 0, "relevance_score": 0.99}, ...]}
"""
from __future__ import annotations

import time

import httpx

from core.config import settings


class Reranker:
    """bge-reranker 精排客户端"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
    ):
        # 地址优先级：显式参数 > RERANK_BASE_URL > 复用 EMBEDDING_BASE_URL 同域
        base = (
            base_url
            or settings.RERANK_BASE_URL
            or settings.EMBEDDING_BASE_URL
        ).strip().rstrip("/")
        if not base:
            raise ValueError("RERANK_BASE_URL / EMBEDDING_BASE_URL 均未配置")
        # 归一化到服务根地址，再拼 /rerank（兼容配置里带 /embeddings 后缀的写法）
        base = base.removesuffix("/embeddings")
        self.url = base + "/rerank"
        self.api_key = api_key or settings.EMBEDDING_KEY
        self.model = model or settings.RERANK_MODEL
        self.max_retries = max_retries
        self.timeout = timeout

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 5,
    ) -> list[dict]:
        """对候选文档精排，返回按相关性降序的结果

        :return: [{"index": 原文档下标, "relevance_score": float}, ...]（最多 top_k 条）
        """
        if not documents:
            return []

        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": min(top_k, len(documents)),
        }
        last_err: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = httpx.post(
                    self.url, json=payload, headers=headers, timeout=self.timeout
                )
                resp.raise_for_status()
                results = resp.json()["results"]
                results.sort(key=lambda r: r["relevance_score"], reverse=True)
                return [
                    {"index": r["index"], "relevance_score": r["relevance_score"]}
                    for r in results
                ]
            except Exception as e:  # noqa: BLE001 —— 网络/限流/5xx 都走重试
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(attempt)

        raise RuntimeError(f"Rerank 请求失败（重试 {self.max_retries} 次）: {last_err}")

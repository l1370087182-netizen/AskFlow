"""Embedding 客户端：OpenAI 兼容接口调 bge-m3，批量请求 + 失败重试。

流水线第二步：文本块 → 1024 维向量。
接口约定（SiliconFlow 等 OpenAI 兼容服务通用）：
    POST {base_url}/embeddings
    {"model": "BAAI/bge-m3", "input": ["文本1", "文本2", ...]}
    返回 {"data": [{"index": 0, "embedding": [...]}, ...]}
"""
from __future__ import annotations

import time

import httpx

from core.config import settings


class EmbeddingClient:
    """OpenAI 兼容 embeddings 接口的轻量客户端"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        batch_size: int = 16,
        max_retries: int = 3,
        timeout: float = 60.0,
    ):
        base = (base_url or settings.EMBEDDING_BASE_URL).strip().rstrip("/")
        if not base:
            raise ValueError("EMBEDDING_BASE_URL 未配置，请检查 .env")
        # 兼容两种写法：配置里带不带 /embeddings 后缀都能用
        self.url = base if base.endswith("/embeddings") else base + "/embeddings"
        self.api_key = api_key or settings.EMBEDDING_KEY
        self.model = model or settings.EMBEDDING_MODEL
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.timeout = timeout

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与输入顺序一一对应的向量列表"""
        if not texts:
            return []
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[i : i + self.batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """单批请求，失败线性退避重试（1s、2s、3s...）"""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model, "input": batch}
        last_err: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = httpx.post(
                    self.url, json=payload, headers=headers, timeout=self.timeout
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                # 按 index 排序，保证向量与输入顺序一一对应
                data.sort(key=lambda d: d["index"])
                return [d["embedding"] for d in data]
            except Exception as e:  # noqa: BLE001 —— 网络/限流/5xx 都走重试
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(attempt)

        raise RuntimeError(f"Embedding 请求失败（重试 {self.max_retries} 次）: {last_err}")

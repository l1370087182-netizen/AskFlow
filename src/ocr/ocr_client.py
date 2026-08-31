"""OCR 客户端：用视觉大模型识别 JD 截图里的文字。

走 OpenAI 兼容的视觉接口（base64 图片塞进 image_url），
与聊天同一个平台（SiliconFlow），默认复用 CHAT/EMBEDDING 的地址与密钥。
"""
from __future__ import annotations

import base64
import time

import httpx

from core.config import settings

OCR_PROMPT = (
    "你是 OCR 文字识别引擎。请把这张图片里的文字完整识别出来，"
    "保持原有结构（换行、编号、列表），原样输出，"
    "不要总结、不要翻译、不要添加任何解释。"
)


class OCRClient:
    """视觉模型文字识别"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ):
        # 地址/密钥优先级：显式参数 > OCR_* > CHAT_* > EMBEDDING_*
        base = (
            base_url
            or settings.OCR_BASE_URL
            or settings.CHAT_BASE_URL
            or settings.EMBEDDING_BASE_URL
        ).strip().rstrip("/")
        if not base:
            raise ValueError("OCR/CHAT/EMBEDDING 的 BASE_URL 均未配置")
        self.url = base.removesuffix("/chat/completions") + "/chat/completions"
        self.api_key = api_key or settings.OCR_KEY or settings.CHAT_KEY or settings.EMBEDDING_KEY
        self.model = model or settings.OCR_MODEL
        if not self.api_key or not self.model:
            raise ValueError("OCR 密钥 / 模型未配置")
        self.timeout = timeout
        self.max_retries = 3

    def recognize(self, image_bytes: bytes, mime: str = "image/png") -> str:
        """识别图片中的文字，返回纯文本（失败线性退避重试，与 Embedding/Rerank 一致）"""
        b64 = base64.b64encode(image_bytes).decode()
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }
            ],
            "max_tokens": 8192,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = httpx.post(
                    self.url, json=body, headers=headers, timeout=self.timeout
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:  # noqa: BLE001 —— 视觉服务偶发 5xx，走重试
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(attempt)
        raise RuntimeError(f"OCR 识别失败（重试 {self.max_retries} 次）: {last_err}")

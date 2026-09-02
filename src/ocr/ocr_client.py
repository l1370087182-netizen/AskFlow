"""OCR 客户端：用视觉大模型识别截图里的文字（双协议，与对话链路一致）。

- OpenAI 兼容：base64 图片塞进 image_url，POST /chat/completions
- Anthropic Messages：base64 图片走 image source，官方 SDK（支持中转站地址）
- 地址/密钥优先级：显式参数 > OCR_* > CHAT_* > EMBEDDING_*
- ⚙️ 个人模型配置优先见 build_ocr_client_for_user
"""
from __future__ import annotations

import base64
import time

import anthropic
import httpx

from core.config import settings

OCR_PROMPT = (
    "你是 OCR 文字识别引擎。请把这张图片里的文字完整识别出来，"
    "保持原有结构（换行、编号、列表），原样输出，"
    "不要总结、不要翻译、不要添加任何解释。"
)

# Anthropic 视觉接口支持的图片类型（不支持 bmp）
ANTHROPIC_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class OCRClient:
    """视觉模型文字识别（provider 分发：openai / anthropic）"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
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
        self.base_url = base
        self.url = base.removesuffix("/chat/completions") + "/chat/completions"
        self.api_key = api_key or settings.OCR_KEY or settings.CHAT_KEY or settings.EMBEDDING_KEY
        self.model = model or settings.OCR_MODEL
        if not self.api_key or not self.model:
            raise ValueError("OCR 密钥 / 模型未配置")
        self.timeout = timeout
        self.max_retries = 3
        self.provider = self._resolve_provider(provider, base)

    @staticmethod
    def _resolve_provider(provider: str | None, base_url: str) -> str:
        """协议识别与 ChatLLM 同规则：显式优先，auto 按地址关键字猜"""
        p = (provider or "auto").strip().lower()
        if p in ("openai", "anthropic"):
            return p
        return "anthropic" if "anthropic" in base_url.lower() else "openai"

    def recognize(self, image_bytes: bytes, mime: str = "image/png") -> str:
        """识别图片中的文字，返回纯文本（按协议分发，失败线性退避重试）"""
        if self.provider == "anthropic":
            return self._recognize_anthropic(image_bytes, mime)
        return self._recognize_openai(image_bytes, mime)

    # ---------- OpenAI 兼容协议 ----------

    def _recognize_openai(self, image_bytes: bytes, mime: str) -> str:
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

        last_err: str | Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = httpx.post(
                    self.url, json=body, headers=headers, timeout=self.timeout
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as e:
                # 带上响应体：模型未开通/不支持图片等平台提示要能直接看到
                last_err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                if e.response.status_code in (401, 403):
                    break  # 鉴权/授权错误，重试不会变好
            except Exception as e:  # noqa: BLE001 —— 视觉服务偶发 5xx，走重试
                last_err = e
            if attempt < self.max_retries:
                time.sleep(attempt)
        raise RuntimeError(f"OCR 识别失败: {last_err}")

    # ---------- Anthropic Messages 协议 ----------

    def _recognize_anthropic(self, image_bytes: bytes, mime: str) -> str:
        if mime not in ANTHROPIC_MIMES:
            raise ValueError(
                f"Anthropic 协议不支持 {mime} 图片，请改用 png/jpg/gif/webp"
            )
        b64 = base64.b64encode(image_bytes).decode()
        last_err: str | Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                client = anthropic.Anthropic(
                    api_key=self.api_key,
                    base_url=self.base_url or None,
                    timeout=self.timeout,
                )
                with client:
                    msg = client.messages.create(
                        model=self.model,
                        max_tokens=8192,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": mime,
                                            "data": b64,
                                        },
                                    },
                                    {"type": "text", "text": OCR_PROMPT},
                                ],
                            }
                        ],
                    )
                return "".join(
                    b.text for b in msg.content if getattr(b, "type", "") == "text"
                ).strip()
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                last_err = f"{getattr(e, 'status_code', '')} {str(e)[:150]}"
                break  # 鉴权/授权错误，重试不会变好
            except Exception as e:  # noqa: BLE001 —— 偶发故障走重试
                last_err = e
            if attempt < self.max_retries:
                time.sleep(attempt)
        raise RuntimeError(f"OCR 识别失败: {last_err}")


def build_ocr_client_for_user(db, uid: int) -> OCRClient:
    """按用户构建 OCR 客户端：⚙️ 个人模型优先（与对话配置同源，网页可改），
    未配置回退服务端 OCR_*/CHAT_*。双协议均可（openai / anthropic）。
    """
    from DAO.user_dao import UserDAO  # 延迟导入，避免模块级循环依赖
    from generation.llm import ChatLLM

    cfg = UserDAO(db).get_llm_config(uid)
    if cfg["base_url"].strip() and cfg["api_key"].strip():
        provider = cfg["provider"] or "auto"
        model = cfg["model"].strip()
        if not model:
            # 模型留空时的默认值与 build_llm_for_user 同口径
            resolved = ChatLLM._resolve_provider(provider, cfg["base_url"])
            model = "claude-opus-4-8" if resolved == "anthropic" else settings.CHAT_MODEL
        return OCRClient(
            base_url=cfg["base_url"].strip(),
            api_key=cfg["api_key"].strip(),
            model=model,
            provider=provider,
        )
    return OCRClient()

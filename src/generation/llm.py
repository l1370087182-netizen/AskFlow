"""Chat 大模型客户端：双协议（OpenAI 兼容 + Anthropic Messages），SSE 流式输出。

通过 .env 三个配置项接入任意一家的模型，由用户自己填：
    CHAT_BASE_URL  —— API 地址
    CHAT_KEY       —— API 密钥
    CHAT_MODEL     —— 模型名
    CHAT_PROVIDER  —— 协议：openai / anthropic / auto（按地址自动识别）

两种协议约定：
- OpenAI 兼容（SiliconFlow、DeepSeek、Moonshot、OpenAI 等）：
    POST {base}/chat/completions
    {"model", "messages", "stream": true}
    流式返回 data: {"choices":[{"delta":{"content":"..."}}]} ... data: [DONE]
- Anthropic Messages（Claude 官方或中转站）：
    POST {base}/v1/messages（官方 SDK 封装）
    system 是独立参数；max_tokens 必填；新 Claude 模型不接受 temperature
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import anthropic
import httpx

from core.config import settings


class ChatLLM:
    """对话大模型客户端，按 provider 分发到两种协议的实现"""

    def __init__(
        self,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ):
        base = (base_url or settings.CHAT_BASE_URL).strip().rstrip("/")
        if not base:
            raise ValueError("CHAT_BASE_URL 未配置，请检查 .env")
        self.base_url = base
        self.api_key = api_key or settings.CHAT_KEY
        self.model = model or settings.CHAT_MODEL
        if not self.api_key or not self.model:
            raise ValueError("CHAT_KEY / CHAT_MODEL 未配置，请检查 .env")
        self.timeout = timeout
        self.provider = self._resolve_provider(
            provider or settings.CHAT_PROVIDER, base
        )

    @staticmethod
    def _resolve_provider(provider: str, base_url: str) -> str:
        """协议识别：显式指定优先；auto 时按地址关键字猜（带 anthropic 走官方协议）"""
        p = (provider or "auto").strip().lower()
        if p in ("openai", "anthropic"):
            return p
        return "anthropic" if "anthropic" in base_url.lower() else "openai"

    # ---------- 统一入口 ----------

    def stream_chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
    ) -> Iterator[str]:
        """流式对话，逐块 yield 文本（两种协议统一成同一种输出）"""
        if self.provider == "anthropic":
            # 新一代 Claude 模型对 temperature 直接返回 400，统一不传
            return self._stream_anthropic(messages)
        return self._stream_openai(messages, temperature)

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        """非流式便捷封装：收集完整回复"""
        return "".join(self.stream_chat(messages, temperature))

    # ---------- OpenAI 兼容协议 ----------

    def _stream_openai(
        self,
        messages: list[dict],
        temperature: float,
    ) -> Iterator[str]:
        """OpenAI 兼容 /chat/completions 流式"""
        url = self.base_url.removesuffix("/chat/completions") + "/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    # SSE 行格式：'data: {...}'，也可能没有空格
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                        delta = data["choices"][0].get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            yield piece
                    except (json.JSONDecodeError, KeyError, IndexError):
                        # 个别心跳/异常块直接跳过
                        continue

    # ---------- Anthropic Messages 协议 ----------

    @staticmethod
    def _to_anthropic_messages(
        messages: list[dict],
    ) -> tuple[str, list[dict]]:
        """把 OpenAI 风格的 messages 转成 Anthropic 需要的形状

        1) system 角色的内容抽出来，拼成独立的 system 参数
        2) 连续同角色消息合并成一条（Anthropic 要求 user/assistant 交替，
           费曼模式结束轮会出现连续两条 user 消息）
        """
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        rest = [m for m in messages if m["role"] != "system"]

        merged: list[dict] = []
        for m in rest:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1] = {
                    **merged[-1],
                    "content": merged[-1]["content"] + "\n\n" + m["content"],
                }
            else:
                merged.append({"role": m["role"], "content": m["content"]})

        return "\n\n".join(system_parts), merged

    def _stream_anthropic(self, messages: list[dict]) -> Iterator[str]:
        """Anthropic Messages 流式（官方 SDK）"""
        system, msgs = self._to_anthropic_messages(messages)

        client = anthropic.Anthropic(
            api_key=self.api_key,
            # 留空时用 SDK 默认官方地址；填了就指向用户自己的地址/中转站
            base_url=self.base_url or None,
            timeout=self.timeout,
        )
        with client:
            with client.messages.stream(
                model=self.model,
                max_tokens=64000,  # 流式请求给足输出上限，避免长回答被截断
                system=system or "You are a helpful assistant.",
                messages=msgs,
            ) as stream:
                for text in stream.text_stream:
                    yield text

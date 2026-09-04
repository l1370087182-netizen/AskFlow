"""JD 分析器：OCR 文本 → 结构化技术栈（调 LLM 输出 JSON）。"""
from __future__ import annotations

import json
import logging
import re

from generation.llm import ChatLLM

from .prompts import JD_ANALYZE_PROMPT

logger = logging.getLogger(__name__)


class JDAnalyzer:
    """把 JD 文本解析成结构化技术栈"""

    def __init__(self, llm: ChatLLM | None = None):
        if llm is None:
            raise ValueError("JDAnalyzer 必须传入用户模型（服务端不再提供默认模型）")
        self.llm = llm

    def analyze(self, jd_text: str) -> dict:
        """分析 JD 文本，返回含 title/summary/tech_stack/soft_requirements 的 dict"""
        prompt = JD_ANALYZE_PROMPT.format(jd_text=jd_text)
        raw = self.llm.chat(
            [{"role": "user", "content": prompt}], temperature=0.2
        )
        data = self._parse_json(raw)
        return self._normalize(data)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """从模型回复解析 JSON，容忍代码块包裹与多余文字"""
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"JD 分析未返回有效 JSON：{raw[:200]}")
        return json.loads(text[start : end + 1])

    @staticmethod
    def _normalize(data: dict) -> dict:
        """补齐缺省字段，过滤非法条目，保证下游落库安全"""
        stack = []
        seen = set()
        for item in data.get("tech_stack", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            level = str(item.get("level", "required")).strip().lower()
            stack.append(
                {
                    "name": name,
                    "category": str(item.get("category", "其他")).strip() or "其他",
                    "level": level if level in ("required", "bonus") else "required",
                    "note": str(item.get("note", "")).strip(),
                }
            )
        return {
            "title": str(data.get("title", "")).strip(),
            "summary": str(data.get("summary", "")).strip(),
            "tech_stack": stack,
            "soft_requirements": [
                str(s).strip()
                for s in data.get("soft_requirements", [])
                if str(s).strip()
            ],
        }

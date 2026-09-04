"""评估器：把费曼模式的评分文本解析成结构化数据并落库。

解析策略（两级）：
1. 正则/分节解析（零成本，覆盖标准格式）
2. 关键字段（分数）解析失败时，调 LLM 兜底提取

落库入口：save_evaluation()，由 chat_controller 在评分产生时调用。
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from DAO.evaluate_dao import EvaluateDAO
from generation.llm import ChatLLM

from .prompts import EXTRACT_PROMPT

logger = logging.getLogger(__name__)

# 标题关键字 → 字段，容忍措辞微调（如「讲对的地方」「答对的部分」）
SECTION_KEYWORDS = [
    ("总结", "summary"),
    ("复述", "summary"),
    ("评分", "score"),
    ("讲对", "correct"),
    ("答对", "correct"),
    ("讲错", "wrong"),
    ("答错", "wrong"),
    ("遗漏", "missed"),
    ("漏掉", "missed"),
]

_LIST_ITEM = re.compile(r"^[-*•]\s+|^\d+[.、)]\s*")
_SCORE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*10")


def _match_section(heading: str) -> str | None:
    """标题文字 → 字段名（按关键字包含匹配）"""
    for kw, field in SECTION_KEYWORDS:
        if kw in heading:
            return field
    return None


def parse_evaluation(text: str) -> dict:
    """正则解析评分 markdown → {score, summary, correct, wrong, missed}

    提不到的字段保持缺省（score=None / 空），由调用方决定是否兜底。
    """
    result: dict = {"score": None, "summary": "", "correct": [], "wrong": [], "missed": []}

    # 1) 分数：全文找一次（通常出现在「掌握度评分：X/10」）
    m = _SCORE.search(text)
    if m:
        result["score"] = float(m.group(1))

    # 2) 按标题分节收集内容
    current = None
    summary_lines: list[str] = []

    def add_item(line: str) -> None:
        item = _LIST_ITEM.sub("", line).strip()
        if not item or item in ("无", "暂无", "没有", "N/A", "-"):
            return
        if current in ("correct", "wrong", "missed"):
            result[current].append(item)
        elif current == "summary":
            summary_lines.append(item)

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or (
            stripped.startswith("**") and stripped.endswith("**")
        ):
            heading = stripped.lstrip("#").strip("*").strip()
            current = _match_section(heading)
            # 「掌握度评分：7/10」这类标题里顺手补一次分数
            if current == "score" and result["score"] is None:
                sm = _SCORE.search(heading)
                if sm:
                    result["score"] = float(sm.group(1))
            continue
        if current in ("correct", "wrong", "missed", "summary"):
            add_item(stripped)
        elif current is None:
            # 没有任何标题的裸文本，归入总结
            summary_lines.append(stripped)

    result["summary"] = "\n".join(summary_lines).strip()
    return result


def _llm_extract(text: str, llm: ChatLLM | None = None) -> dict:
    """LLM 兜底：格式太自由时让模型提取结构化结果（用调用方的用户模型，
    服务端无默认模型；llm 为 None 时直接抛错由上层保留正则结果）"""
    if llm is None:
        raise ValueError("无可用模型，跳过 LLM 兜底提取")
    prompt = EXTRACT_PROMPT.format(evaluation_text=text)
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 兜底提取未返回 JSON：{raw[:200]}")
    data = json.loads(raw[start : end + 1])
    return {
        "score": data.get("score"),
        "summary": str(data.get("summary", "")).strip(),
        "correct": [str(x).strip() for x in data.get("correct", []) if str(x).strip()],
        "wrong": [str(x).strip() for x in data.get("wrong", []) if str(x).strip()],
        "missed": [str(x).strip() for x in data.get("missed", []) if str(x).strip()],
    }


def extract_structured(text: str, llm: ChatLLM | None = None) -> dict:
    """两级提取：先正则，分数缺失再走 LLM 兜底"""
    parsed = parse_evaluation(text)
    if parsed["score"] is None:
        try:
            parsed = _llm_extract(text, llm)
        except Exception as e:  # noqa: BLE001 —— 兜底失败就保留正则结果
            logger.warning("[evaluate] LLM 兜底提取失败，保留正则结果：%s", e)
    return parsed


def save_evaluation(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    topic: str,
    rounds: int,
    evaluation_text: str,
    llm: ChatLLM | None = None,
):
    """解析评分文本并写入 evaluate 表，返回记录行（通知挂钩取分数用）。

    :param llm: 正则解析不出分数时的 LLM 兜底（传用户模型；None=只用正则）
    """
    structured = extract_structured(evaluation_text, llm)
    dao = EvaluateDAO(db)
    row = dao.create(
        user_id=user_id,
        session_id=session_id,
        topic=topic,
        rounds=rounds,
        score=structured.get("score"),
        summary=structured.get("summary", ""),
        correct_points=json.dumps(structured.get("correct", []), ensure_ascii=False),
        wrong_points=json.dumps(structured.get("wrong", []), ensure_ascii=False),
        missed_points=json.dumps(structured.get("missed", []), ensure_ascii=False),
        raw=evaluation_text,
    )
    return row

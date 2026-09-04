"""模拟面试分析器：简历结构化 + JD-简历差距计算 + 总评弱项解析。

差距 = JD 要求（required）但简历没体现的技术 → 即「推荐学习」清单，
不依赖答题质量，确定性强、可解释。
弱项 = 总评「## 薄弱点」章节的条目（正则解析，失败才调模型兜底）。
"""
from __future__ import annotations

import json
import logging
import re

from generation.llm import ChatLLM

from .prompts import RESUME_EXTRACT_PROMPT, WEAKNESS_EXTRACT_PROMPT

logger = logging.getLogger(__name__)


class InterviewAnalyzer:
    def __init__(self, llm: ChatLLM | None = None):
        if llm is None:
            raise ValueError("InterviewAnalyzer 必须传入用户模型（服务端不再提供默认模型）")
        self.llm = llm

    def extract_resume(self, resume_text: str) -> dict:
        """简历 OCR 文本 → {name, skills, projects}"""
        # 提示词用双大括号转义，.format() 还原成单大括号 JSON 示例
        prompt = RESUME_EXTRACT_PROMPT.format()
        raw = self.llm.chat(
            [{"role": "user", "content": f"{prompt}\n\n【简历文本】\n{resume_text}"}],
            temperature=0.2,
        )
        return self._parse_json(raw) or {"name": "", "skills": [], "projects": []}

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def jd_points_text(jd_analysis: dict) -> str:
        """把 JD 技术栈转成面试官可读的考点列表"""
        lines = []
        for t in jd_analysis.get("tech_stack", []):
            lines.append(f"- {t['name']}（{t['category']}，{t['level']}）")
        return "\n".join(lines) or "（JD 未提取到明确技术栈，请围绕通用后端/AI 知识提问）"

    @staticmethod
    def resume_brief_text(resume: dict) -> str:
        skills = "、".join(resume.get("skills", [])) or "（未提取到技能）"
        projs = "；".join(
            f"{p.get('name', '')}（{p.get('tech', '')}）"
            for p in resume.get("projects", [])[:5]
        )
        return f"技能：{skills}\n项目：{projs or '（无）'}"

    @staticmethod
    def compute_gap(jd_analysis: dict, resume: dict) -> list[str]:
        """JD required 技术里，简历技能没覆盖的 → 推荐学习主题"""
        skills = {s.strip().lower() for s in resume.get("skills", []) if s}
        gap = []
        for t in jd_analysis.get("tech_stack", []):
            if t.get("level") != "required":
                continue
            name = t["name"].strip()
            if not name or name.lower() in skills:
                continue
            # 简单包含匹配（简历里写了 "FastAPI" 覆盖 "FastAPI"）
            if any(name.lower() in s or s in name.lower() for s in skills):
                continue
            gap.append(name)
        return gap


# ---------- 总评弱项解析 ----------

def extract_weaknesses(summary: str, llm: ChatLLM | None = None) -> list[str]:
    """从总评文本解析薄弱知识点：正则解析「## 薄弱点」章节，失败才调模型兜底。

    :return: 弱项主题列表（≤10 条，短句）
    """
    items = _parse_weakness_md(summary)
    if items:
        return items
    if llm is None:
        return []
    try:
        raw = llm.chat(
            [{"role": "user", "content": WEAKNESS_EXTRACT_PROMPT + "\n\n" + summary[:4000]}],
            temperature=0.1,
        )
        return _parse_weakness_json(raw)
    except Exception as e:  # noqa: BLE001 —— 解析失败不影响面试结果落库
        logger.warning("[interview] 弱项 LLM 兜底解析失败：%s", e)
        return []


def _parse_weakness_md(summary: str) -> list[str]:
    """解析总评 Markdown 的「薄弱点」章节条目"""
    m = re.search(r"##\s*薄弱点\s*\n(.*?)(?=\n##|\Z)", summary or "", re.S)
    if not m:
        return []
    items = re.findall(r"^\s*[-*]\s+(.+?)\s*$", m.group(1), re.M)
    return [i.strip(" .。·") for i in items if i.strip()][:10]


def _parse_weakness_json(raw: str) -> list[str]:
    """宽容解析模型兜底输出（["主题", ...] 或 {"weaknesses": [...]}）"""
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    candidates = [t]
    start, end = t.find("["), t.rfind("]")
    if start >= 0 and end > start:
        candidates.append(t[start : end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            data = data.get("weaknesses")
        if isinstance(data, list):
            out = [str(x).strip() for x in data if str(x).strip()]
            return out[:10]
    return []

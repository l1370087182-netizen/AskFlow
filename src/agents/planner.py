"""Planner Agent：学习规划（阶段 1③ 起步：面试驱动）。

输入（payload）：{interview_record_id}
流程：读面试记录（弱项+缺口）→ 逐主题查知识库命中 → LLM 产出结构化计划
     → output 存 items + 知识引用 + Markdown 全文，前端可直接渲染。

定位（设计文档 §4.2）：单实例、低频（用户主动触发）、大模型。
知识库检索是"编排检索"思想的复用：优先用库内知识填缺口，缺失的在
建议里提示「可提交爬取」（学习任务板阶段 2 会自动发爬取子任务）。
"""
import json
import logging
import re

from DAO.agent_task_dao import AgentTaskDAO
from agent_engine.base_agent import BaseAgent, TaskPermanentError
from generation.llm import build_llm_for_user
from interview.prompts import PLANNER_PROMPT
from milvus.retrieval.hybird import HybridRetriever
from model.AgentTaskModel import TaskKind

logger = logging.getLogger(__name__)

MAX_ITEMS = 6          # 计划条目上限（与提示词一致）
REFS_PER_TOPIC = 2     # 每个主题附带的知识库引用数


class PlannerAgent(BaseAgent):
    """学习规划：面试记录 → 结构化学习计划"""

    KINDS = [TaskKind.STUDY_PLAN]

    def process(self, task, db) -> None:
        from model.InterviewRecordModel import InterviewRecordModel  # 延迟导入防环

        record_id = (task.payload or {}).get("interview_record_id")
        dao = AgentTaskDAO(db)

        rec = (
            db.query(InterviewRecordModel)
            .filter(InterviewRecordModel.id == record_id)
            .first()
        )
        if rec is None or rec.user_id != task.user_id:
            raise TaskPermanentError("面试记录不存在或非本人")

        llm = build_llm_for_user(db, task.user_id)
        if llm is None:
            raise TaskPermanentError("未配置个人大模型，请先到「对话学习」页 ⚙️ 配置模型")

        weaknesses = rec.weaknesses or []
        gaps = rec.gap_topics or []
        if not weaknesses and not gaps:
            dao.write_back(
                task.id, self.agent_id, task.version,
                output={"items": [], "plan_md": "本次面试没有识别出明确弱项与缺口，暂无需补课计划。"},
                log_action="complete", log_desc="无弱项/缺口，空计划",
            )
            return

        # 逐主题检索知识库（命中 → 附引用；检索失败不影响规划）
        refs = self._collect_refs(db, task.user_id, weaknesses, gaps)

        prompt = PLANNER_PROMPT.format(
            jd_title=rec.jd_title or "（未命名岗位）",
            weaknesses="、".join(weaknesses[:8]) or "（无）",
            gaps="、".join(gaps[:8]) or "（无）",
        )
        try:
            raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
        except Exception as e:  # noqa: BLE001 —— 模型调用失败属偶发，走引擎重试
            raise

        items = self._parse_items(raw)
        plan_md = self._render_md(rec.jd_title, items, refs)

        dao.write_back(
            task.id, self.agent_id, task.version,
            output={"items": items, "knowledge_refs": refs, "plan_md": plan_md},
            log_action="complete",
            log_desc=f"学习计划已生成：{len(items)} 个条目",
        )
        logger.info(
            "[planner:%s] 面试记录 %s 的学习计划已生成（%s 条目）",
            self.agent_id, record_id, len(items),
        )

    # ---------- 知识引用 ----------

    @staticmethod
    def _collect_refs(db, uid: int, weaknesses: list, gaps: list) -> dict:
        """逐主题检索知识库，返回 {主题: [{knowledge_id, title}]}"""
        refs: dict = {}
        try:
            retriever = HybridRetriever(db)
        except Exception as e:  # noqa: BLE001 —— 检索栈不可用则无引用
            logger.warning("[planner] 检索器初始化失败（计划不带引用）：%s", e)
            return refs
        for topic in list(weaknesses) + list(gaps):
            if topic in refs or len(refs) >= MAX_ITEMS:
                continue
            try:
                hits = retriever.search(topic, top_k=REFS_PER_TOPIC, uid=uid)
            except Exception as e:  # noqa: BLE001 —— 单主题失败跳过
                logger.warning("[planner] 主题「%s」检索失败：%s", topic, e)
                continue
            refs[topic] = [
                {"knowledge_id": h.get("knowledge_id"), "title": (h.get("content") or "")[:40]}
                for h in hits if h.get("knowledge_id")
            ]
        return refs

    # ---------- 解析 / 渲染 ----------

    @staticmethod
    def _parse_items(raw: str) -> list[dict]:
        """宽容解析计划条目（剥围栏/截花括号），失败返回空列表"""
        t = (raw or "").strip()
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
        candidates = [t]
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            candidates.append(t[start : end + 1])
        for c in candidates:
            try:
                data = json.loads(c)
            except json.JSONDecodeError:
                continue
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            out = []
            for it in items[:MAX_ITEMS]:
                if not isinstance(it, dict):
                    continue
                topic = str(it.get("topic", "")).strip()
                if not topic:
                    continue
                out.append({
                    "topic": topic,
                    "priority": str(it.get("priority", "medium")).strip().lower(),
                    "reason": str(it.get("reason", "")).strip(),
                    "suggestion": str(it.get("suggestion", "")).strip(),
                })
            return out
        return []

    @staticmethod
    def _render_md(jd_title: str, items: list[dict], refs: dict) -> str:
        """把结构化条目渲染成前端可直接展示的 Markdown"""
        if not items:
            return "未能生成结构化计划（模型输出不可解析），请直接参考总评薄弱点自学。"
        prio_zh = {"high": "高", "medium": "中", "low": "低"}
        lines = [f"## 学习计划 · {jd_title or '面试补强'}", ""]
        for i, it in enumerate(items, 1):
            lines.append(f"### {i}. {it['topic']}（优先级：{prio_zh.get(it['priority'], '中')}）")
            if it.get("reason"):
                lines.append(f"- **为什么补**：{it['reason']}")
            if it.get("suggestion"):
                lines.append(f"- **怎么学**：{it['suggestion']}")
            topic_refs = refs.get(it["topic"]) or []
            if topic_refs:
                titles = "、".join(f"《{r['title']}》" for r in topic_refs)
                lines.append(f"- **知识库已有**：{titles}")
            else:
                lines.append("- **知识库暂无**：可提交爬取补充，或自行查找官方文档")
            lines.append("")
        return "\n".join(lines)

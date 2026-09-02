"""Reviewer Agent：爬取内容质检（审核与生产分离——不审自己的活）。

输入（payload）：{knowledge_ids: [...], crawl_task_id}
流程：逐条 规则层先筛 → 规则拿不准才调模型复核 → keep/discard
- discard：删知识行 + 删向量（同用户删除语义），理由留痕
- keep 的条目 → 发 term_curate 子任务接力给 curator
模型缺位时退化为纯规则审核（保守：拿不准就留）。
"""
import json
import logging
import re

from DAO.agent_task_dao import AgentTaskDAO
from DAO.knowledge_dao import KnowledgeDAO
from agent_engine.base_agent import BaseAgent, TaskPermanentError  # noqa: F401
from generation.llm import build_llm_for_user
from milvus.ingestion.VectorStore import get_vector_store
from model.AgentTaskModel import TaskKind
from model.KnowledgeModel import KnowledgeModel

logger = logging.getLogger(__name__)

# 规则层：导航/模板残留标记词（命中种类多 + 正文短 → 判定垃圾页）
_BOILERPLATE_MARKERS = (
    "下一页", "上一页", "导航", "索引", "版权", "copyright", "logo",
    "跳转到内容", "搜索", "菜单", "登录", "注册", "cookie",
)

REVIEW_PROMPT = (
    "你是知识库内容质检员。判断下面这段爬取内容是否为有价值的技术知识：\n"
    "- keep：有实质技术内容（概念解释/用法说明/代码示例/API 文档/教程）\n"
    "- discard：导航页/目录索引/登录页/广告/404/模板文字堆砌/与标题完全无关的噪声\n"
    "拿不准时倾向 keep（宁可留，不可错删）。\n"
    "只输出 JSON：{\"verdict\": \"keep\"或\"discard\", \"reason\": \"30字以内理由\"}"
)


class ReviewerAgent(BaseAgent):
    """质检员：规则优先，LLM 兜底，单实例（审核工作量小，排队即可）"""

    KINDS = [TaskKind.QUALITY_REVIEW]

    def process(self, task, db) -> None:
        knowledge_ids = (task.payload or {}).get("knowledge_ids", [])
        dao = AgentTaskDAO(db)
        kb_dao = KnowledgeDAO(db)
        llm = build_llm_for_user(db, task.user_id)  # 可为 None → 纯规则

        kept, discarded, details = [], [], []
        for kid in knowledge_ids[:50]:  # 单任务审核上限，防异常超长
            row = kb_dao.get_by_db(kid)
            if row is None or row.user_id != task.user_id:
                continue  # 已删除/越权：跳过
            verdict, reason = self._review_row(row, llm)
            if verdict == "discard":
                self._discard(kb_dao, row)
                discarded.append(kid)
            else:
                kept.append(kid)
            if len(details) < 20:
                details.append({"knowledge_id": kid, "verdict": verdict, "reason": reason})

        # 写回 + 接力：幸存条目发术语整理子任务
        output = {"kept": len(kept), "discarded": len(discarded), "details": details}
        dao.write_back(
            task.id, self.agent_id, task.version,
            output=output,
            log_action="complete",
            log_desc=f"质检完成：保留 {len(kept)} / 丢弃 {len(discarded)}",
        )
        if kept:
            dao.create(
                kind=TaskKind.TERM_CURATE,
                user_id=task.user_id,
                payload={"knowledge_ids": kept},
                parent_id=task.id,
                trace_id=task.trace_id,
                agent=self.agent_id,
            )
        logger.info(
            "[reviewer:%s] 任务 %s 质检完成：保留 %s / 丢弃 %s",
            self.agent_id, task.id, len(kept), len(discarded),
        )

    # ---------- 判定 ----------

    def _review_row(self, row: KnowledgeModel, llm) -> tuple[str, str]:
        """单条判定：规则层 → 拿不准交给模型；模型缺位/失败保守保留"""
        rule = self._rule_verdict(row)
        if rule is not None:
            return rule
        if llm is None:
            return "keep", "规则无法判定且无可用模型，保守保留"
        try:
            raw = llm.chat(
                [{"role": "user", "content": self._build_prompt(row)}],
                temperature=0.1,
            )
            data = self._parse(raw)
            if data and data.get("verdict") in ("keep", "discard"):
                return data["verdict"], str(data.get("reason", ""))[:60] or "模型判定"
        except Exception as e:  # noqa: BLE001 —— 模型失败保守保留
            logger.warning("[reviewer] LLM 判定失败（保守保留）：%s", e)
        return "keep", "模型输出无法解析，保守保留"

    @staticmethod
    def _rule_verdict(row: KnowledgeModel) -> tuple[str, str] | None:
        """规则层：明确垃圾才丢，拿不准返回 None 交模型。

        :return: (verdict, reason) 或 None（不确定）
        """
        content = row.content or ""
        if len(content) < 200:
            return "discard", "正文过短，无知识价值"
        head = content[:3000].lower()
        hits = sum(1 for m in _BOILERPLATE_MARKERS if m.lower() in head)
        # 短正文 + 大量导航模板词 → 抓取到的是导航壳而非正文
        if len(content) < 400 and hits >= 3:
            return "discard", "正文以导航/模板文字为主"
        return None

    @staticmethod
    def _build_prompt(row: KnowledgeModel) -> str:
        return (
            REVIEW_PROMPT
            + f"\n\n标题：{row.title}\n分类：{row.category}"
            + f"\n正文（前2000字）：\n{(row.content or '')[:2000]}"
        )

    @staticmethod
    def _parse(raw: str):
        """宽容解析模型输出（剥围栏/截最外层花括号）"""
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
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _discard(kb_dao: KnowledgeDAO, row: KnowledgeModel) -> None:
        """丢弃：先删向量再删行（向量失败不阻塞，同用户删除接口语义）"""
        try:
            get_vector_store().delete_by_knowledge(row.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[reviewer] 删除向量失败（继续删行）：id=%s %s", row.id, e)
        kb_dao.delete(row.id)

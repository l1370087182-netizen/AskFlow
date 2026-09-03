"""Reviewer Agent：爬取内容质检（审核与生产分离——不审自己的活）。

质量门禁哲学（宁缺毋滥）：规则层先筛 → 拿不准调 LLM 打知识价值分 →
分数 ≥ QUALITY_MIN_SCORE 才留。与旧版「拿不准倾向 keep」相反，这里严格把关。

唯一例外：LLM 评分失败（限流/服务抖动）绝不误删——删除不可逆。此时保留内容
但 quality_score 记 NULL（"待补审"），留痕 llm_failed，可由
scripts/review_personal_kb.py 回补重评。

输入（payload）：{knowledge_ids: [...], crawl_task_id, backfill?}
流程：逐条 规则层 → LLM 评分 → keep 写分 / discard 删行删向量 → 接力 term_curate
"""
import logging

from DAO.agent_task_dao import AgentTaskDAO
from DAO.knowledge_dao import KnowledgeDAO
from agent_engine.base_agent import BaseAgent, TaskPermanentError  # noqa: F401
from agents.quality import QUALITY_MIN_SCORE, rule_verdict, score_content
from generation.llm import build_llm_for_user
from milvus.ingestion.VectorStore import get_vector_store
from model.AgentTaskModel import TaskKind
from model.KnowledgeModel import KnowledgeModel

logger = logging.getLogger(__name__)


class ReviewerAgent(BaseAgent):
    """质检员：规则优先，LLM 严格评分，单实例（审核工作量小，排队即可）"""

    KINDS = [TaskKind.QUALITY_REVIEW]

    def process(self, task, db) -> None:
        knowledge_ids = (task.payload or {}).get("knowledge_ids", [])
        backfill = bool((task.payload or {}).get("backfill"))
        self._note(f"质检 {len(knowledge_ids)} 条知识" + ("（存量补审）" if backfill else ""))
        dao = AgentTaskDAO(db)
        kb_dao = KnowledgeDAO(db)
        llm = build_llm_for_user(db, task.user_id)  # 可为 None → 纯规则+保守保留

        kept, discarded, details = [], [], []
        for kid in knowledge_ids[:50]:  # 单任务审核上限（补审脚本按 ≤50 分片）
            row = kb_dao.get_by_db(kid)
            if row is None or row.user_id != task.user_id:
                continue  # 已删除/越权：跳过
            verdict, reason, score = self._review_row(row, llm)
            if verdict == "discard":
                self._discard(kb_dao, row)
                discarded.append(kid)
                # 删除理由条条可追溯：discard 不设条数上限
                details.append({
                    "knowledge_id": kid, "verdict": "discard",
                    "score": score, "reason": reason,
                })
            else:
                # keep：写入质量分（LLM 失败时 score=None = 待补审）
                kb_dao.update_quality(kid, score, reason)
                kept.append(kid)
                if sum(1 for d in details if d["verdict"] == "keep") < 20:
                    details.append({
                        "knowledge_id": kid, "verdict": "keep",
                        "score": score, "reason": reason,
                    })

        # 写回 + 接力：幸存条目发术语整理子任务
        output = {"kept": len(kept), "discarded": len(discarded), "details": details}
        if backfill:
            output["backfill"] = True
        dao.write_back(
            task.id, self.agent_id, task.version,
            output=output,
            log_action="complete",
            log_desc=(
                f"{'存量补审' if backfill else '质检'}完成："
                f"保留 {len(kept)} / 丢弃 {len(discarded)}"
            ),
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

    def _review_row(self, row: KnowledgeModel, llm) -> tuple[str, str, float | None]:
        """单条判定：规则层 → LLM 严格评分。

        :return: (verdict, reason, score)；LLM 失败时 score=None（保留待补审，不误删）
        """
        # 1) 规则层：确信垃圾直接丢
        rule = rule_verdict(row.content)
        if rule is not None:
            return "discard", rule[1], None
        # 2) 无模型：保守保留（待补审）
        if llm is None:
            return "keep", "规则无法判定且无可用模型，保留待补审", None
        # 3) LLM 打知识价值分，≥ 阈值才留
        score, reason = score_content(llm, row.title, row.category, row.content)
        if score is None:
            # 评分失败：绝不误删，保留 + 记 llm_failed + 分数留 NULL（待补审）
            return "keep", f"llm_failed：{reason}", None
        if score >= QUALITY_MIN_SCORE:
            return "keep", reason, score
        return "discard", f"知识价值不足（{score:.0f} 分）：{reason}", score

    @staticmethod
    def _discard(kb_dao: KnowledgeDAO, row: KnowledgeModel) -> None:
        """丢弃：先删向量再删行（向量失败不阻塞，同用户删除接口语义）"""
        try:
            get_vector_store().delete_by_knowledge(row.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[reviewer] 删除向量失败（继续删行）：id=%s %s", row.id, e)
        kb_dao.delete(row.id)

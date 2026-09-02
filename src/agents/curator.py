"""Curator Agent：从质检合格的个人知识中提炼术语，注册进用户作用域术语表。

输入（payload）：{knowledge_ids: [...]}
- 每篇提炼 ≤3 个核心术语（含别名/一句话简介），归一化判重后注册，
  user_id=任务归属者（个人术语只进本人每日卡片，不泄漏到全局）
- 术语表是「锦上添花」：无可用模型/提炼失败 → 任务照常完成不失败
"""
import json
import logging
import re

from DAO.agent_task_dao import AgentTaskDAO
from DAO.knowledge_dao import KnowledgeDAO
from DAO.tech_term_dao import TechTermDAO
from agent_engine.base_agent import BaseAgent
from generation.llm import build_llm_for_user
from model.AgentTaskModel import TaskKind

logger = logging.getLogger(__name__)

MAX_TERMS_PER_DOC = 3      # 单篇提炼上限
MAX_DOCS_PER_TASK = 20     # 单任务处理篇数上限

CURATE_PROMPT = (
    "从下面的技术文档中提炼最核心的技术术语（最多 {max_terms} 个）：\n"
    "- term：简洁术语（不超过 20 字，优先英文专有名词或通行中文译名）\n"
    "- alias：常见别名/缩写（逗号分隔，没有就留空字符串）\n"
    "- brief：一句话通俗解释（不超过 60 字）\n"
    "只提炼文档真正讲解的概念，不要提炼导航词/网站名。\n"
    "只输出 JSON：{{\"terms\": [{{\"term\": \"...\", \"alias\": \"...\", \"brief\": \"...\"}}]}}"
)


class CuratorAgent(BaseAgent):
    """术语整理：单实例懒消费（接力触发，不做常驻巡检）"""

    KINDS = [TaskKind.TERM_CURATE]

    def process(self, task, db) -> None:
        knowledge_ids = (task.payload or {}).get("knowledge_ids", [])
        dao = AgentTaskDAO(db)

        llm = build_llm_for_user(db, task.user_id)
        if llm is None:
            dao.write_back(
                task.id, self.agent_id, task.version,
                output={"skipped": "无可用模型，未提炼术语"},
                log_action="complete", log_desc="无模型，跳过术语提炼",
            )
            return

        kb_dao = KnowledgeDAO(db)
        term_dao = TechTermDAO(db)
        registered, dup = 0, 0

        for kid in knowledge_ids[:MAX_DOCS_PER_TASK]:
            row = kb_dao.get_by_db(kid)
            if row is None or row.user_id != task.user_id:
                continue
            for item in self._extract_terms(llm, row):
                created = term_dao.create_if_absent(
                    term=item["term"],
                    alias=item.get("alias", ""),
                    category=row.category or "general",
                    brief=item.get("brief", ""),
                    source_url=row.source_url if not row.source_url.startswith("manual://") else "",
                    user_id=task.user_id,
                )
                if created:
                    registered += 1
                else:
                    dup += 1

        dao.write_back(
            task.id, self.agent_id, task.version,
            output={"registered": registered, "duplicate": dup},
            log_action="complete",
            log_desc=f"术语提炼完成：新增 {registered} / 已有 {dup}",
        )
        logger.info(
            "[curator:%s] 任务 %s 术语提炼完成：新增 %s / 已有 %s",
            self.agent_id, task.id, registered, dup,
        )

    # ---------- 提炼 ----------

    def _extract_terms(self, llm, row) -> list[dict]:
        """LLM 提炼术语；失败返回空（不影响任务完成）"""
        prompt = CURATE_PROMPT.format(max_terms=MAX_TERMS_PER_DOC) + (
            f"\n\n标题：{row.title}\n分类：{row.category}"
            f"\n正文（前1500字）：\n{(row.content or '')[:1500]}"
        )
        try:
            raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        except Exception as e:  # noqa: BLE001
            logger.warning("[curator] 提炼调用失败（跳过该篇）：%s", e)
            return []
        data = self._parse(raw)
        if not data or not isinstance(data.get("terms"), list):
            return []
        out = []
        for t in data["terms"][:MAX_TERMS_PER_DOC]:
            if not isinstance(t, dict):
                continue
            term = str(t.get("term", "")).strip()
            if not term or len(term) > 40:
                continue
            out.append({
                "term": term,
                "alias": str(t.get("alias", "")).strip()[:200],
                "brief": str(t.get("brief", "")).strip()[:120],
            })
        return out

    @staticmethod
    def _parse(raw: str):
        """宽容解析（剥围栏/截最外层花括号）"""
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

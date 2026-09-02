"""Planner Agent：学习规划（大模型、低频、单实例）。

承担三类任务（设计文档 §4.2）：
- study_plan   面试记录 → 结构化学习计划（§1③，面试页「生成学习计划」）
- learning_goal 任务板：用户目标 / 面试计划上板 → 拆解为 learning_item 子任务
- learning_item 任务板：单个学习子题 → 基于知识库资料编写学习材料

共同特点：逐主题检索知识库（优先用库内知识填缺口；编排检索思想的复用）。
"""
import json
import logging
import re

from DAO.agent_task_dao import AgentTaskDAO
from DAO.knowledge_dao import KnowledgeDAO
from agent_engine.base_agent import BaseAgent, TaskPermanentError
from generation.llm import build_llm_for_user
from interview.prompts import PLANNER_PROMPT
from milvus.retrieval.hybird import HybridRetriever
from model.AgentTaskModel import TaskKind

logger = logging.getLogger(__name__)

MAX_ITEMS = 6          # 目标拆解/计划条目上限
REFS_PER_TOPIC = 2     # 每个主题附带的知识库引用数
REF_CONTEXT_CHARS = 1500  # 编材料时每个引用带入的正文长度

# 任务板：目标拆解
GOAL_DECOMPOSE_PROMPT = """你是技术学习规划助手。用户发布了一个学习目标，请拆解成 3-6 个循序渐进的学习子题：
- 每个子题聚焦一个知识点，按「先基础后进阶」排序
- topic 简洁（不超过 20 字）；priority 按学习顺序给 high/medium/low
- reason 一句话说明为什么学；suggestion 给具体学法（看什么/写什么）
只输出 JSON：
{"items": [{"topic": "...", "priority": "high", "reason": "...", "suggestion": "..."}]}"""

# 任务板：单题学习材料
ITEM_MATERIAL_PROMPT = """你是技术导师。请基于【知识库资料】为学习主题编写一篇通俗的讲解材料：

要求：
1. 先用一句话定义这个主题是什么；
2. 再讲核心原理/关键要点（分点）；
3. 有资料代码的给出典型用法示例；
4. 资料不足的地方明确说出来，并建议补充方向；
5. 用 Markdown 输出，控制在 600 字以内，面向有一定基础的学习者。

【学习主题】{topic}
【学习建议】{suggestion}

【知识库资料】{context}"""


class PlannerAgent(BaseAgent):
    """学习规划：面试计划 + 任务板目标拆解与材料编写"""

    KINDS = [TaskKind.STUDY_PLAN, TaskKind.LEARNING_GOAL, TaskKind.LEARNING_ITEM]

    def process(self, task, db) -> None:
        if task.kind == TaskKind.STUDY_PLAN:
            self._process_study_plan(task, db)
        elif task.kind == TaskKind.LEARNING_GOAL:
            self._process_goal(task, db)
        elif task.kind == TaskKind.LEARNING_ITEM:
            self._process_item(task, db)

    # ---------- ① 面试记录 → 学习计划（面试页入口） ----------

    def _process_study_plan(self, task, db) -> None:
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

        refs = self._collect_refs(db, task.user_id, weaknesses, gaps)

        prompt = PLANNER_PROMPT.format(
            jd_title=rec.jd_title or "（未命名岗位）",
            weaknesses="、".join(weaknesses[:8]) or "（无）",
            gaps="、".join(gaps[:8]) or "（无）",
        )
        raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
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

    # ---------- ② 任务板：目标 → 拆解子题 ----------

    def _process_goal(self, task, db) -> None:
        payload = task.payload or {}
        dao = AgentTaskDAO(db)
        goal_text = str(payload.get("goal", "")).strip()
        items: list[dict] = []

        record_id = payload.get("interview_record_id")
        if record_id:
            # 面试计划上板：弱项+缺口直接作为学习条目（确定性拆解，不花调用）
            from model.InterviewRecordModel import InterviewRecordModel

            rec = (
                db.query(InterviewRecordModel)
                .filter(InterviewRecordModel.id == record_id)
                .first()
            )
            if rec is None or rec.user_id != task.user_id:
                raise TaskPermanentError("面试记录不存在或非本人")
            if not goal_text:
                goal_text = f"面试补强 · {rec.jd_title or '模拟面试'}"
            for i, w in enumerate(rec.weaknesses or []):
                items.append({
                    "topic": w, "priority": "high",
                    "reason": "面试答题暴露的薄弱点", "suggestion": "",
                })
            for g in rec.gap_topics or []:
                items.append({
                    "topic": g, "priority": "medium",
                    "reason": "JD 要求但简历未覆盖", "suggestion": "",
                })
        else:
            # 用户自由目标：LLM 拆解
            llm = build_llm_for_user(db, task.user_id)
            if llm is None:
                raise TaskPermanentError("未配置个人大模型，请先到「对话学习」页 ⚙️ 配置模型")
            raw = llm.chat(
                [{"role": "user", "content": GOAL_DECOMPOSE_PROMPT + f"\n\n【学习目标】{goal_text}"}],
                temperature=0.3,
            )
            items = self._parse_items(raw)
            if not items:
                raise TaskPermanentError("目标拆解失败：模型输出无法解析")

        items = items[:MAX_ITEMS]
        if not items:
            raise TaskPermanentError("没有可拆解的学习条目")

        # 逐题检索知识库引用，随子任务携带
        refs = self._collect_refs(db, task.user_id, [i["topic"] for i in items], [])

        # 创建子任务（parent 显式挂边），目标任务随即完成
        # order 记录取拆解顺序（先基础后进阶）——任务 id 是随机 hex，顺序不能靠 id
        out_items = []
        for i, it in enumerate(items):
            child = dao.create(
                kind=TaskKind.LEARNING_ITEM,
                user_id=task.user_id,
                payload={
                    "topic": it["topic"],
                    "priority": it.get("priority", "medium"),
                    "reason": it.get("reason", ""),
                    "suggestion": it.get("suggestion", ""),
                    "goal": goal_text,
                    "order": i,
                    "refs": refs.get(it["topic"], []),
                },
                parent_id=task.id,
                trace_id=task.trace_id,
                agent=self.agent_id,
            )
            out_items.append({**it, "task_id": child.id})

        dao.write_back(
            task.id, self.agent_id, task.version,
            output={"goal": goal_text, "items": out_items},
            log_action="complete",
            log_desc=f"目标已拆解为 {len(out_items)} 个学习子题",
        )
        logger.info(
            "[planner:%s] 目标 %s 已拆解：%s 个子题", self.agent_id, task.id, len(out_items)
        )

    # ---------- ③ 任务板：单题 → 学习材料 ----------

    def _process_item(self, task, db) -> None:
        p = task.payload or {}
        topic = str(p.get("topic", "")).strip() or "（未命名主题）"
        dao = AgentTaskDAO(db)

        llm = build_llm_for_user(db, task.user_id)
        if llm is None:
            raise TaskPermanentError("未配置个人大模型，请先到「对话学习」页 ⚙️ 配置模型")

        # 引用资料拉正文（全局或本人条目，他人个人块引用不到——检索时就过滤过）
        kb_dao = KnowledgeDAO(db)
        context_parts = []
        for ref in (p.get("refs") or [])[:3]:
            row = kb_dao.get_by_db(ref.get("knowledge_id"))
            if row is None or row.user_id not in (0, task.user_id):
                continue
            context_parts.append(f"【资料：{row.title}】\n{row.content[:REF_CONTEXT_CHARS]}")
        context = "\n\n".join(context_parts) or "（知识库暂无相关资料）"

        prompt = ITEM_MATERIAL_PROMPT.format(
            topic=topic,
            suggestion=p.get("suggestion") or "（无特别建议）",
            context=context,
        )
        material = llm.chat([{"role": "user", "content": prompt}], temperature=0.4).strip()

        dao.write_back(
            task.id, self.agent_id, task.version,
            output={"topic": topic, "material_md": material or "（生成失败，请重试）"},
            log_action="complete",
            log_desc=f"学习材料已生成（{len(context_parts)} 份参考资料）",
        )
        logger.info("[planner:%s] 学习子题 %s 材料已生成", self.agent_id, task.id)

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

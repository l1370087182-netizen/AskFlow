"""Chain 组装：系统提示 + 会话历史 + 检索上下文 + 当前消息 → messages。

讲解模式：每条消息都即时检索（问题即查询）；传入 llm 且编排开关开启时
走编排检索（多查询改写+融合，见 retrieval_orchestrator，全程可降级）。
费曼模式：只在选题时检索一次，结果作为「参考答案」藏进系统提示，不直接展示。
术语兜底：tech_term（卡片数据源）与 knowledge 语料是两条独立链路，术语常常
「卡片里有、语料里没有」——消息里命中术语时，把术语卡片一并拼进上下文。
图谱扩展（§7.9）：命中的术语若在知识图谱里有强共现邻居，把邻居实体拼进
BM25 查询做扩展（只扩 BM25，向量查询保持原问题）；图谱为空/异常时零影响。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from core.config import settings
from milvus.retrieval.hybird import HybridRetriever, relevant_hits

from .prompts import (
    ASK_SYSTEM_PROMPT,
    MAX_TEACH_ROUNDS,
    TEACH_EVAL_PROMPT,
    TEACH_EVAL_SYSTEM_PROMPT,
    TEACH_OPENING_PROMPT,
    TEACH_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


def format_context(results: list[dict]) -> str:
    """把检索结果格式化成编号片段，拼进提示词"""
    if not results:
        return "（未检索到相关片段）"
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"[片段 {i}]（分类：{r.get('category', '')}，相关度：{r.get('score', 0):.2f}）\n"
            f"{r['content']}"
        )
    return "\n\n".join(parts)


def format_term_card(term) -> str:
    """术语 → 参考片段里的【术语卡片】文本块（tech_term 表的兜底内容）"""
    lines = []
    head = f"【术语卡片】{term.term}"
    if term.alias:
        head += f"（别名：{term.alias}）"
    lines.append(head)
    if term.brief:
        lines.append(f"一句话简介：{term.brief}")
    if term.detail:
        lines.append(f"详细讲解：{term.detail}")
    if term.example:
        lines.append(f"示例：{term.example}")
    if term.source_url:
        lines.append(f"来源：{term.source_url}")
    return "\n".join(lines)


class ChainBuilder:
    """负责把提示词、历史、检索结果组装成最终 messages"""

    def __init__(self, db: Session, retriever: HybridRetriever | None = None):
        self.db = db
        self.retriever = retriever or HybridRetriever(db)

    # ---------- 术语兜底 ----------

    def match_term(self, message: str, uid: int = 0):
        """在消息里找用户可见的术语（词边界匹配，防 'ai' 误伤 'main'）。

        多个术语同时命中时取名字最长的（更具体），如「依赖注入」优先于「注入」。
        """
        from DAO.tech_term_dao import TechTermDAO  # 延迟导入，与 user_dao 同理

        msg = message.lower()
        best = None  # (名字长度, term)
        for t in TechTermDAO(self.db).list_visible(uid):
            names = [t.term] + [a.strip() for a in (t.alias or "").split(",")]
            for name in names:
                if not name:
                    continue
                if re.search(rf"(?<![0-9a-z]){re.escape(name.lower())}(?![0-9a-z])", msg):
                    if best is None or len(name) > best[0]:
                        best = (len(name), t)
        return best[1] if best else None

    def term_context(self, message: str, uid: int = 0) -> str:
        """消息命中术语时返回「术语卡片」文本块（拼进参考片段末尾），否则空串"""
        term = self.match_term(message, uid)
        return format_term_card(term) if term else ""

    def kg_expansion(self, term, uid: int = 0) -> list[str]:
        """命中术语 → 图谱强邻居实体名（BM25 查询扩展词）。

        图谱是渐进增强：查不到节点/边、表还没建、任何异常都返回空，
        检索照常按原问题执行。
        """
        if term is None:
            return []
        try:
            from DAO.kg_dao import KgDAO  # 延迟导入，与 user_dao 同理

            return KgDAO(self.db).expansion_terms(uid, term)
        except Exception as e:  # noqa: BLE001 —— 扩展是增强，绝不阻断检索
            logger.warning("[chain] 图谱查询扩展失败（返回空）：%s", e)
            return []

    # ---------- 讲解模式 ----------

    def build_ask(
        self,
        message: str,
        history: list[dict],
        top_k: int = 5,
        uid: int = 0,
        llm=None,
    ) -> tuple[list[dict], list[dict]]:
        """组装讲解模式 messages：即时检索当前问题

        :param uid: 请求者用户；检索时全局块+本人个人块可见（个人知识库）
        :param llm: 传入且编排开关开启时走编排检索（多查询改写+融合），
                任何异常自动回退普通检索
        :return: (messages, 检索结果) —— 检索结果另外用于前端展示引用来源；
                已经相关度阈值过滤（hybird.relevant_hits），空列表即「知识库无资料」
        """
        # 术语兜底 + 图谱扩展：命中术语卡片时，顺带取图谱邻居实体扩进 BM25
        # 查询（一次 match_term 两个用途）；没命中/图谱为空 → 检索原样进行
        term = self.match_term(message, uid)
        term_card = format_term_card(term) if term else ""
        results = relevant_hits(
            self._search_ask(message, top_k, uid, llm, self.kg_expansion(term, uid))
        )
        context = format_context(results)
        if term_card:
            context += "\n\n" + term_card
        system = ASK_SYSTEM_PROMPT.format(context=context)
        messages = (
            [{"role": "system", "content": system}]
            + history
            + [{"role": "user", "content": message}]
        )
        return messages, results

    def _search_ask(self, message: str, top_k: int, uid: int, llm, expand: list[str] | None = None) -> list[dict]:
        """讲解模式检索：编排开关开启且有 llm 时走编排器，否则普通检索

        :param expand: 图谱查询扩展词（可能为空列表），只影响 BM25 路
        """
        if llm is not None and settings.RETRIEVAL_ORCHESTRATOR:
            try:
                from generation.retrieval_orchestrator import RetrievalOrchestrator

                return RetrievalOrchestrator(self.retriever, llm).search(
                    message, top_k=top_k, uid=uid, expand=expand
                )
            except Exception as e:  # noqa: BLE001 —— 编排异常回退普通检索
                logger.warning("[chain] 编排检索异常，回退普通检索：%s", e)
        return self.retriever.search(message, top_k=top_k, uid=uid, expand=expand)

    # ---------- 费曼模式 ----------

    def build_teach_opening(self, session_data: dict) -> list[dict]:
        """选题成功后的开场轮：学生自我介绍 + 邀请老师开讲"""
        meta = session_data["meta"]
        system = TEACH_SYSTEM_PROMPT.format(
            topic=meta["topic"],
            reference=meta.get("reference", "（无参考答案）"),
            round_info="这是开场，还没有开始提问。",
        )
        opening = TEACH_OPENING_PROMPT.format(topic=meta["topic"])
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": opening},
        ]

    def build_teach(
        self,
        session_data: dict,
        message: str,
        finish: bool = False,
        include_message: bool = True,
    ) -> list[dict]:
        """组装费曼模式追问/总结轮 messages

        :param finish: True 时在末尾追加总结评分指令
        :param include_message: 是否带上用户本轮消息
                （用户只发了「结束」这类关键词时不带，避免污染评分）
        """
        meta = session_data["meta"]
        rounds = meta.get("rounds", 0)

        if finish:
            # 评分轮切换到「评分员」系统提示，退出学生角色，避免人设对抗评分指令
            system = TEACH_EVAL_SYSTEM_PROMPT.format(
                topic=meta.get("topic", ""),
                reference=meta.get("reference", "（无参考答案）"),
            )
        else:
            round_info = f"你已经提了 {rounds} 轮问题（最多 {MAX_TEACH_ROUNDS} 轮）。"
            system = TEACH_SYSTEM_PROMPT.format(
                topic=meta.get("topic", ""),
                reference=meta.get("reference", "（无参考答案）"),
                round_info=round_info,
            )
        messages = [{"role": "system", "content": system}] + session_data["messages"]
        if include_message:
            messages.append({"role": "user", "content": message})
        if finish:
            messages.append({"role": "user", "content": TEACH_EVAL_PROMPT})
        return messages

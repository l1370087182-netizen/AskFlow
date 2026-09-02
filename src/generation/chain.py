"""Chain 组装：系统提示 + 会话历史 + 检索上下文 + 当前消息 → messages。

讲解模式：每条消息都即时检索（问题即查询）；传入 llm 且编排开关开启时
走编排检索（多查询改写+融合，见 retrieval_orchestrator，全程可降级）。
费曼模式：只在选题时检索一次，结果作为「参考答案」藏进系统提示，不直接展示。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from core.config import settings
from milvus.retrieval.hybird import HybridRetriever

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


class ChainBuilder:
    """负责把提示词、历史、检索结果组装成最终 messages"""

    def __init__(self, db: Session, retriever: HybridRetriever | None = None):
        self.retriever = retriever or HybridRetriever(db)

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
        :return: (messages, 检索结果) —— 检索结果另外用于前端展示引用来源
        """
        results = self._search_ask(message, top_k, uid, llm)
        system = ASK_SYSTEM_PROMPT.format(context=format_context(results))
        messages = (
            [{"role": "system", "content": system}]
            + history
            + [{"role": "user", "content": message}]
        )
        return messages, results

    def _search_ask(self, message: str, top_k: int, uid: int, llm) -> list[dict]:
        """讲解模式检索：编排开关开启且有 llm 时走编排器，否则普通检索"""
        if llm is not None and settings.RETRIEVAL_ORCHESTRATOR:
            try:
                from generation.retrieval_orchestrator import RetrievalOrchestrator

                return RetrievalOrchestrator(self.retriever, llm).search(
                    message, top_k=top_k, uid=uid
                )
            except Exception as e:  # noqa: BLE001 —— 编排异常回退普通检索
                logger.warning("[chain] 编排检索异常，回退普通检索：%s", e)
        return self.retriever.search(message, top_k=top_k, uid=uid)

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

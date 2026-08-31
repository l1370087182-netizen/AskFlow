"""evaluate 表：费曼模式的讲解评分记录（阶段 9）。

一条记录 = 一次「选题 → 讲解 → 总结评分」的完整评估，
由 chat_controller 在评分产生时联动写入。
"""
from database.session import Base
from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class EvaluateModel(Base):
    """费曼讲解评分记录"""

    __tablename__ = "evaluate"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="所属会话"
    )
    topic: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", index=True, comment="讲解主题"
    )
    rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="追问轮数"
    )
    # 掌握度评分 0-10，解析失败时为 None
    score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="掌握度评分（0-10）"
    )
    summary: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="",
        comment="总结复述",
    )
    # 以下三个清单以 JSON 数组字符串存储
    correct_points: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="[]",
        comment="讲对的知识点（JSON数组）",
    )
    wrong_points: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="[]",
        comment="讲错的知识点（JSON数组）",
    )
    missed_points: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="[]",
        comment="遗漏的知识点（JSON数组）",
    )
    raw: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="",
        comment="评分原文（模型输出的 markdown）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )

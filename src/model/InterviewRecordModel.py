"""interview_record 面试记录表：模拟面试结果落库（设计文档 §1④）。

面试结束不再丢数据：总评/弱项/缺口/逐轮记录全部持久化，
作为学习任务规划（planner）与任务板的输入源。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class InterviewRecordModel(Base):
    __tablename__ = "interview_record"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="所属用户"
    )
    jd_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="关联 jd 表（JD 分析复用）"
    )
    jd_title: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="面试岗位标题"
    )
    rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="实际面试轮数"
    )
    final_summary: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False, default="", comment="总评原文（Markdown）",
    )
    # 结构化弱项：["Milvus 索引原理", ...]（总评解析而来）
    weaknesses: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="薄弱知识点列表"
    )
    # JD 要求但简历未覆盖的缺口：["Kafka", ...]
    gap_topics: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="JD-简历缺口列表"
    )
    resume_skills: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="简历技能（快照）"
    )
    # 逐轮问答：[{role, content}, ...]
    transcript: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="逐轮问答记录"
    )
    # 最近一次学习计划任务 id（agent_task），便于记录页直接查计划
    plan_task_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="最近学习计划任务 id"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="面试时间"
    )

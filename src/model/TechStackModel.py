"""tech_stack 表：从 JD 中提取出的技术栈条目，一次分析对应多条。"""
from database.session import Base
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class TechStackModel(Base):
    """技术栈条目：隶属于某条 jd 记录"""

    __tablename__ = "tech_stack"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    jd_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="所属 jd 记录 id"
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="技术名称（如 Python / FastAPI）"
    )
    # 语言 / 后端框架 / 数据库 / 向量库 / 中间件 / DevOps / AI框架 / 其他
    category: Mapped[str] = mapped_column(
        String(64), nullable=False, default="其他", comment="技术分类"
    )
    # required=任职要求，加分项；bonus=优先/加分
    level: Mapped[str] = mapped_column(
        String(32), nullable=False, default="required", comment="必需还是加分项"
    )
    note: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="JD 原文语境说明"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )

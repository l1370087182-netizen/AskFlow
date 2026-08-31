"""tech_term 技术术语表：每日学习卡片的数据源。

对应 CLAUDE.md §5.2：
    term       术语（如 aigc）
    alias      别名（逗号分隔）
    category   技术分类
    brief      一句话简介
    source_url 来源链接
"""
from database.session import Base
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class TechTermModel(Base):
    """技术术语：每日学习卡片从这里抽一条展示"""

    __tablename__ = "tech_term"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    # 术语本身，唯一（如 aigc）
    term: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, comment="术语"
    )
    # 别名，逗号分隔（如 "AI生成内容,人工智能生成内容"）
    alias: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="别名（逗号分隔）"
    )
    category: Mapped[str] = mapped_column(
        String(128), nullable=False, default="general", comment="技术分类"
    )
    # 一句话简介，卡片正文
    brief: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", comment="一句话简介"
    )
    # 详细讲解：原理/应用场景的通俗展开
    detail: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="",
        comment="详细讲解",
    )
    # 示例：代码或场景举例
    example: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="",
        comment="示例（代码或场景）",
    )
    source_url: Mapped[str] = mapped_column(
        String(768), nullable=False, default="", comment="来源链接"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
        comment="更新时间",
    )

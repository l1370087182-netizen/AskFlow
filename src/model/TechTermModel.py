"""tech_term 技术术语表：每日学习卡片的数据源。

对应 CLAUDE.md §5.2：
    user_id    归属：0=全局术语（种子/全局语料提炼）；>0=用户个人术语
               （个人知识爬取提炼，仅本人卡片可见，防学习内容泄漏）
    term       术语（如 aigc）
    alias      别名（逗号分隔）
    category   技术分类
    brief      一句话简介
    source_url 来源链接

唯一性：同一归属下术语不重复（user_id + term）；
存量表迁移（单列 unique term → 复合）见 scripts/init_db.py（幂等）。
"""
from database.session import Base
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class TechTermModel(Base):
    """技术术语：每日学习卡片从这里抽一条展示"""

    __tablename__ = "tech_term"
    __table_args__ = (
        # 同一归属下术语不重复（跨归属可各自拥有同名术语）
        UniqueConstraint("user_id", "term", name="uq_term_user_term"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True,
        comment="归属；0=全局术语，>0=用户个人术语",
    )
    # 术语本身（如 aigc），归属内唯一
    term: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="术语"
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

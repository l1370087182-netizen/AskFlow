"""知识图谱两张表：实体节点（kg_node）+ 共现边（kg_edge）。轻量建图。

对应 CLAUDE.md §7.9：
- 节点来源：reviewer 质检 LLM 评分时搭车抽取的实体（一次调用两份产出），
  匹配得上 tech_term 的节点挂 term_id（与术语卡片/查询扩展联动）
- 边：同一篇知识内共同出现的实体对（cooccur），weight 累计共现次数
- 归属：user_id 0=全局（全局语料质检产出），>0=个人（个人知识质检产出），
  语义与 tech_term 一致——个人图谱仅本人可见，防学习内容泄漏
"""
from database.session import Base
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class KgNodeModel(Base):
    """实体节点：从质检合格知识里抽出的技术概念"""

    __tablename__ = "kg_node"
    __table_args__ = (
        # 同一归属下实体名不重复
        UniqueConstraint("user_id", "name", name="uq_kg_node_user_name"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True,
        comment="归属；0=全局，>0=个人（个人图谱仅本人可见）",
    )
    # 实体名（如 FastAPI / 依赖注入），归属内唯一
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="实体名"
    )
    category: Mapped[str] = mapped_column(
        String(128), nullable=False, default="general", comment="技术分类"
    )
    # 命中的 tech_term.id（归一化匹配得上才挂；NULL=图谱独立概念）
    term_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None, index=True, comment="关联 tech_term.id"
    )
    # 累计被抽中的篇数（重复出现只加计数，不重复建行）
    mention_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="被抽中的篇数"
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


class KgEdgeModel(Base):
    """共现边：同一篇知识内共同出现的实体对（无向，src_id < dst_id 存一份）"""

    __tablename__ = "kg_edge"
    __table_args__ = (
        UniqueConstraint("user_id", "src_id", "dst_id", "relation",
                         name="uq_kg_edge_pair"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True, comment="归属；0=全局，>0=个人"
    )
    src_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("kg_node.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="起点节点 id（较小）",
    )
    dst_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("kg_node.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="终点节点 id（较大）",
    )
    relation: Mapped[str] = mapped_column(
        String(32), nullable=False, default="cooccur", comment="关系类型（当前仅 cooccur）"
    )
    # 共现次数（同对实体每共同出现一篇 +1）
    weight: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="共现次数"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )

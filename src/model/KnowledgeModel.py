from database.session import Base
from sqlalchemy import UniqueConstraint, Integer, String, Text, DateTime
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

class KnowledgeModel(Base):
    """知识库模型：爬虫采集/文档上传的原始知识"""

    STATUS_PENDING = 0    # 待向量化
    STATUS_EMBEDDED = 1   # 已写入向量库
    STATUS_FAILED = 2     # 向量化失败

    __tablename__ = "knowledge"
    
    # 同一URL不重复入库
    __table_args__ = (
        UniqueConstraint("source_url", name="uq_knowledge_source_url"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    title: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="标题"
    )
    # MySQL 下用 LONGTEXT：TEXT 只有 64KB，长页面（如 release notes）会超限
    content: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        comment="正文内容",
    )
    # 来源链接
    source_url: Mapped[str] = mapped_column(
        String(768), nullable=False, default="", comment="来源链接"
    )
    # 技术分类
    category: Mapped[str] = mapped_column(
        String(128), nullable=False, default="general", comment="技术分类"
    )
    # 来源类型
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="spider", comment="来源类型"
    )
    # 0=待向量化，1=已向量化，2=向量化失败
    status: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="处理状态"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
        comment="更新时间"
    )


    

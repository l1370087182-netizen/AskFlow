from database.session import Base
from sqlalchemy import Index, Integer, String, Text, DateTime
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

class KnowledgeModel(Base):
    """知识库模型：爬虫采集/文档上传的原始知识 + 用户个人知识

    归属（个人知识库功能）：
    - user_id=0 是全局哨兵：爬虫/旧上传/手动创建的全局共享语料
    - user_id>0 是该用户的个人知识（手工添加/整站爬取），仅本人可见可检索
    - 唯一约束为复合 (user_id, source_url)：不同用户可收藏同一 URL，
      全局语料维持原语义。存量表迁移见 scripts/init_db.py（幂等）。
    """

    STATUS_PENDING = 0    # 待向量化
    STATUS_EMBEDDED = 1   # 已写入向量库
    STATUS_FAILED = 2     # 向量化失败

    # 全局知识归属哨兵值（不用 NULL：复合唯一约束下 NOT NULL 语义更干净）
    GLOBAL_USER_ID = 0

    __tablename__ = "knowledge"

    # 同一用户下同一 URL 不重复入库（替换原单列 uq_knowledge_source_url）。
    # 注意：source_url 是 varchar(768)，utf8mb4 下整列入索引会顶破 InnoDB
    # 3072 字节索引上限，复合索引必须用前缀 source_url(700)。
    # SQLAlchemy 2.0 的 MySQL 方言不给 UniqueConstraint 收 mysql_length，
    # 用「唯一 Index + mysql_length」表达同样的复合唯一约束；
    # DAO 层保留全值判重兜底（索引前缀 700 之外的极端长尾靠应用层）。
    __table_args__ = (
        Index(
            "uq_knowledge_user_url",
            "user_id",
            "source_url",
            unique=True,
            mysql_length={"source_url": 700},
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        index=True,
        comment="所属用户；0=全局知识",
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


    

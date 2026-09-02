"""知识库 DAO：全局语料（user_id=0）与个人知识（user_id>0）统一存取。

个人知识库功能的归属约定：
- 全部查询带 user_id 维度；复合唯一键 (user_id, source_url) 由数据库保证
- source_url(700) 只是索引前缀，DAO 层判重始终用全值比较兜底
- 全局接口必须显式传 user_id=0（默认值），杜绝个人条目泄漏进全局视图
"""
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from model.KnowledgeModel import KnowledgeModel


class KnowledgeDAO:
    """知识库DAO,负责知识库的增删改查"""

    def __init__(self, db: Session):
        self.db = db

    def get_by_db(self, knowledge_id: int) -> KnowledgeModel | None:
        """按照主键查询"""
        return (
            self.db.query(KnowledgeModel)
            .filter(KnowledgeModel.id == knowledge_id)
            .first()
        )

    def get_by_url(self, url: str, user_id: int = 0) -> KnowledgeModel | None:
        """按照 (来源链接, 归属用户) 查询；默认查全局语料"""
        return (
            self.db.query(KnowledgeModel)
            .filter(
                KnowledgeModel.source_url == url,
                KnowledgeModel.user_id == user_id,
            )
            .first()
        )

    def list_by_category(
        self,
        category: str | None = None,
        status: int | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int = 0,
    ) -> list[KnowledgeModel]:
        """按照分类查询；user_id=0 为全局视图，>0 为该用户的个人列表"""
        q = self.db.query(KnowledgeModel).filter(KnowledgeModel.user_id == user_id)
        if category:
            q = q.filter(KnowledgeModel.category == category)
        if status is not None:
            q = q.filter(KnowledgeModel.status == status)
        return q.order_by(KnowledgeModel.id.desc()).offset(offset).limit(limit).all()

    def count_by_category(
        self,
        category: str | None = None,
        status: int | None = None,
        user_id: int = 0,
    ) -> int:
        """与 list_by_category 同口径的计数（分页 total 用）"""
        q = self.db.query(func.count(KnowledgeModel.id)).filter(
            KnowledgeModel.user_id == user_id
        )
        if category:
            q = q.filter(KnowledgeModel.category == category)
        if status is not None:
            q = q.filter(KnowledgeModel.status == status)
        return q.scalar()

    def list_pending(self, limit: int | None = None) -> list[KnowledgeModel]:
        """查询所有待向量化（status=0）的知识，按 id 升序，供向量化流水线使用

        不区分归属：全局与个人条目统一入库，归属信息随块写入 Milvus。
        """
        q = (
            self.db.query(KnowledgeModel)
            .filter(KnowledgeModel.status == KnowledgeModel.STATUS_PENDING)
            .order_by(KnowledgeModel.id)
        )
        if limit:
            q = q.limit(limit)
        return q.all()

    def category_counts(self, user_id: int = 0) -> list[tuple[str, int]]:
        """按分类聚合条数，返回 [(category, count), ...] 按数量降序

        全局视图（/categories）必须传 user_id=0，避免泄漏个人条目。
        """
        rows = (
            self.db.query(
                KnowledgeModel.category,
                func.count(KnowledgeModel.id),
            )
            .filter(KnowledgeModel.user_id == user_id)
            .group_by(KnowledgeModel.category)
            .order_by(func.count(KnowledgeModel.id).desc())
            .all()
        )
        return [(c, n) for c, n in rows]

    def create(
        self,
        *,
        title: str,
        content: str,
        source_url: str = "",
        category: str = "general",
        source_type: str = "spider",
        status: int = KnowledgeModel.STATUS_PENDING,
        user_id: int = 0,
    ) -> KnowledgeModel | None:
        """新增知识，若同一归属下 source_url 已存在，则返回None"""
        if source_url and self.get_by_url(source_url, user_id):
            return None

        row = KnowledgeModel(
            title=title,
            content=content,
            source_url=source_url,
            category=category,
            source_type=source_type,
            status=status,
            user_id=user_id,
        )
        self.db.add(row)

        try:
            self.db.commit()
            self.db.refresh(row)
            return row
        except IntegrityError as e:
            self.db.rollback()
            print(f"新增知识失败: {e}")
            return None

    def upsert(
        self,
        *,
        title: str,
        content: str,
        source_url: str = "",
        category: str = "general",
        source_type: str = "spider",
        user_id: int = 0,
    ) -> KnowledgeModel | None:
        """新增或更新知识：以 (user_id, source_url) 命中；内容/标题有变化才覆盖，
        并把 status 重置为待向量化（块内容变了必须重建向量）"""
        row = self.get_by_url(source_url, user_id) if source_url else None

        if row is None:
            # URL在该归属下不存在，新建
            row = KnowledgeModel(
                title=title,
                content=content,
                source_url=source_url,
                category=category,
                source_type=source_type,
                status=KnowledgeModel.STATUS_PENDING,
                user_id=user_id,
            )
            self.db.add(row)
        elif row.content != content or row.title != title:
            # 内容或标题有变化，更新
            row.content = content
            row.title = title
            row.category = category
            row.source_type = source_type
            row.status = KnowledgeModel.STATUS_PENDING
        # 内容和标题都没有变化，不更新
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_content(
        self,
        knowledge_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
    ) -> KnowledgeModel | None:
        """编辑条目（个人知识 PUT 用）：任一字段有变化 → status 重置为 0。

        category 也存进 Milvus 块（检索时按类过滤），所以改分类同样要重建向量。
        返回更新后的行；无任何变化时原样返回（status 不动）。
        """
        row = self.get_by_db(knowledge_id)
        if not row:
            return None
        changed = False
        if title is not None and title != row.title:
            row.title = title
            changed = True
        if content is not None and content != row.content:
            row.content = content
            changed = True
        if category is not None and category != row.category:
            row.category = category
            changed = True
        if changed:
            row.status = KnowledgeModel.STATUS_PENDING
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_status(self, knowledge_id: int, status: int) -> bool:
        """更新知识状态"""
        row = self.get_by_db(knowledge_id)
        if not row:
            return False
        row.status = status
        self.db.commit()
        return True

    def delete(self, knowledge_id: int) -> bool:
        """删除知识"""
        row = self.get_by_db(knowledge_id)
        if not row:
            return False
        self.db.delete(row)
        self.db.commit()
        return True

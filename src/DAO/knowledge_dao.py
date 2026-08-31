from sqlalchemy.orm import Session
from model.KnowledgeModel import KnowledgeModel
from sqlalchemy.exc import IntegrityError

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

    def get_by_url(self, url: str) -> KnowledgeModel | None:
        """按照来源链接查询"""
        return (
            self.db.query(KnowledgeModel)
            .filter(KnowledgeModel.source_url == url)
            .first()
        )

    def list_by_category(
        self,
        category: str | None = None,
        status: int | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[KnowledgeModel]:
        """按照分类查询"""
        q = self.db.query(KnowledgeModel)
        if category:
            q = q.filter(KnowledgeModel.category == category)
        if status is not None:
            q = q.filter(KnowledgeModel.status == status)
        return q.order_by(KnowledgeModel.id.desc()).offset(offset).limit(limit).all()

    def list_pending(self, limit: int | None = None) -> list[KnowledgeModel]:
        """查询所有待向量化（status=0）的知识，按 id 升序，供向量化流水线使用"""
        q = (
            self.db.query(KnowledgeModel)
            .filter(KnowledgeModel.status == KnowledgeModel.STATUS_PENDING)
            .order_by(KnowledgeModel.id)
        )
        if limit:
            q = q.limit(limit)
        return q.all()

    def create(
        self,
        *,
        title: str,
        content: str,
        source_url: str = "",
        category: str = "general",
        source_type: str = "spider",
        status: int = KnowledgeModel.STATUS_PENDING,
    ) -> KnowledgeModel | None:
        """新增知识，若source_url已存在，则返回None"""
        if source_url and self.get_by_url(source_url):
            return None
        
        row = KnowledgeModel(
            title=title,
            content=content,
            source_url=source_url,
            category=category,
            source_type=source_type,
            status=status,
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
    ) -> KnowledgeModel | None:
        """新增或更新知识，若source_url已存在，并且有变化，则更新"""
        row = self.get_by_url(source_url) if source_url else None

        if row is None:
            # URL不存在，新建
            row = KnowledgeModel(
                title=title,
                content=content,
                source_url=source_url,
                category=category,
                source_type=source_type,
                status=KnowledgeModel.STATUS_PENDING,
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




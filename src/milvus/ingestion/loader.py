"""加载器：从 MySQL 读取待向量化（status=0）的知识。

流水线第一步：只负责取数据，不做任何加工。
"""
from sqlalchemy.orm import Session

from DAO.knowledge_dao import KnowledgeDAO
from model.KnowledgeModel import KnowledgeModel


class KnowledgeLoader:
    """从 knowledge 表加载待向量化条目"""

    def __init__(self, db: Session):
        self.dao = KnowledgeDAO(db)

    def load_pending(self, limit: int | None = None) -> list[KnowledgeModel]:
        """取 status=0 的知识，按 id 升序（先入库先处理）

        :param limit: 只取前 N 条，None 表示全部
        """
        return self.dao.list_pending(limit=limit)

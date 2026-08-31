"""tech_term 表的 DAO：术语的增查。"""
from sqlalchemy.orm import Session

from model.TechTermModel import TechTermModel


class TechTermDAO:
    """技术术语 DAO"""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _normalize(term: str) -> str:
        """归一化：忽略大小写与空格，防止 'Static Files' / 'StaticFiles' 重复入库"""
        return term.lower().replace(" ", "").replace("-", "")

    def get_by_term(self, term: str) -> TechTermModel | None:
        """按术语精确查询"""
        return (
            self.db.query(TechTermModel)
            .filter(TechTermModel.term == term)
            .first()
        )

    def exists_normalized(self, term: str) -> bool:
        """归一化判重（术语表很小，全量比对足够快）"""
        target = self._normalize(term)
        return any(self._normalize(t.term) == target for t in self.list_all())

    def list_all(self) -> list[TechTermModel]:
        """全部术语（每日卡片从中随机抽取）"""
        return (
            self.db.query(TechTermModel)
            .order_by(TechTermModel.id)
            .all()
        )

    def count(self) -> int:
        """术语总数"""
        return self.db.query(TechTermModel).count()

    def create_if_absent(
        self,
        *,
        term: str,
        alias: str = "",
        category: str = "general",
        brief: str = "",
        source_url: str = "",
        detail: str = "",
        example: str = "",
    ) -> TechTermModel | None:
        """术语不存在才创建（归一化判重）；已存在返回 None（幂等，重复灌种子安全）"""
        if self.exists_normalized(term):
            return None
        row = TechTermModel(
            term=term,
            alias=alias,
            category=category,
            brief=brief,
            source_url=source_url,
            detail=detail,
            example=example,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_missing_detail(self) -> list[TechTermModel]:
        """还没有详细讲解的术语（供补全脚本使用）"""
        return (
            self.db.query(TechTermModel)
            .filter(TechTermModel.detail == "")
            .order_by(TechTermModel.id)
            .all()
        )

    def update_enrichment(self, term_id: int, detail: str, example: str) -> None:
        """回填详细讲解与示例"""
        row = self.db.query(TechTermModel).filter(TechTermModel.id == term_id).first()
        if not row:
            return
        row.detail = detail
        row.example = example
        self.db.commit()

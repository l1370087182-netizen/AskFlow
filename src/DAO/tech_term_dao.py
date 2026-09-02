"""tech_term 表的 DAO：术语的增查（含用户归属维度）。"""
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

    def get_by_term(self, term: str, user_id: int = 0) -> TechTermModel | None:
        """按术语精确查询（指定归属）"""
        return (
            self.db.query(TechTermModel)
            .filter(TechTermModel.term == term, TechTermModel.user_id == user_id)
            .first()
        )

    def list_visible(self, uid: int) -> list[TechTermModel]:
        """用户可见术语 = 全局术语 + 本人个人术语（每日卡片抽取范围）"""
        return (
            self.db.query(TechTermModel)
            .filter(TechTermModel.user_id.in_([0, uid]))
            .order_by(TechTermModel.id)
            .all()
        )

    def exists_normalized(self, term: str, user_id: int = 0) -> bool:
        """归一化判重：在「全局 + 该归属」范围内比对。

        全局已有 → 个人不必再存一份（卡片本就可见）；
        本人已有 → 幂等跳过。术语表很小，全量比对足够快。
        """
        target = self._normalize(term)
        scope = self.list_visible(user_id) if user_id else self.list_all()
        return any(self._normalize(t.term) == target for t in scope)

    def list_all(self) -> list[TechTermModel]:
        """全部术语（管理/脚本用；用户卡片请用 list_visible）"""
        return (
            self.db.query(TechTermModel)
            .order_by(TechTermModel.id)
            .all()
        )

    def count(self, user_id: int | None = None) -> int:
        """术语总数；传 user_id 只计该归属（0=全局）"""
        q = self.db.query(TechTermModel)
        if user_id is not None:
            q = q.filter(TechTermModel.user_id == user_id)
        return q.count()

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
        user_id: int = 0,
    ) -> TechTermModel | None:
        """术语不存在才创建（归属内+全局归一化判重）；已存在返回 None（幂等）"""
        if not term.strip():
            return None
        if self.exists_normalized(term, user_id):
            return None
        row = TechTermModel(
            term=term.strip(),
            alias=alias,
            category=category,
            brief=brief,
            source_url=source_url,
            detail=detail,
            example=example,
            user_id=user_id,
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

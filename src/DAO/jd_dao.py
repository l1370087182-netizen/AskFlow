"""jd / tech_stack 表的 DAO。"""
from sqlalchemy.orm import Session

from model.JDModel import JDModel
from model.TechStackModel import TechStackModel


class JDDAO:
    """JD 分析记录与技术栈的增查"""

    def __init__(self, db: Session):
        self.db = db

    def create(self, *, user_id: int, filename: str, image_path: str) -> JDModel:
        """分析开始时先落一条记录（OCR/分析结果后续回填）"""
        row = JDModel(user_id=user_id, filename=filename, image_path=image_path)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def save_result(
        self,
        jd_id: int,
        *,
        ocr_text: str,
        title: str,
        summary: str,
        analysis_raw: str,
        tech_stack: list[dict],
    ) -> None:
        """回填 OCR 文本、分析结果，并批量写入技术栈条目

        用不带用户过滤的 _get_by_id_raw：本方法只在 analyze 同一请求内、
        create 之后立即调用，属主已在上游鉴权确认，无越权风险。
        """
        jd = self._get_by_id_raw(jd_id)
        if not jd:
            return
        jd.ocr_text = ocr_text
        jd.title = title
        jd.summary = summary
        jd.analysis_raw = analysis_raw

        for item in tech_stack:
            self.db.add(
                TechStackModel(
                    jd_id=jd_id,
                    name=item["name"],
                    category=item["category"],
                    level=item["level"],
                    note=item["note"],
                )
            )
        self.db.commit()

    def _get_by_id_raw(self, jd_id: int) -> JDModel | None:
        """仅内部回填用：不按用户过滤（调用方已确认属主）"""
        return self.db.query(JDModel).filter(JDModel.id == jd_id).first()

    def get_by_id(self, jd_id: int, user_id: int) -> JDModel | None:
        """按主键 + 属主查；越权查询返回 None"""
        return (
            self.db.query(JDModel)
            .filter(JDModel.id == jd_id, JDModel.user_id == user_id)
            .first()
        )

    def get_stack(self, jd_id: int) -> list[TechStackModel]:
        return (
            self.db.query(TechStackModel)
            .filter(TechStackModel.jd_id == jd_id)
            .order_by(TechStackModel.id)
            .all()
        )

    def list_recent(self, user_id: int, limit: int = 20, offset: int = 0) -> list[JDModel]:
        return (
            self.db.query(JDModel)
            .filter(JDModel.user_id == user_id)
            .order_by(JDModel.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

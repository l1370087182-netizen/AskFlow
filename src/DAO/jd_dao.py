"""jd / tech_stack 表的 DAO。"""
from sqlalchemy.orm import Session

from model.JDModel import JDModel
from model.TechStackModel import TechStackModel


class JDDAO:
    """JD 分析记录与技术栈的增查"""

    def __init__(self, db: Session):
        self.db = db

    def create(self, *, filename: str, image_path: str) -> JDModel:
        """分析开始时先落一条记录（OCR/分析结果后续回填）"""
        row = JDModel(filename=filename, image_path=image_path)
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
        """回填 OCR 文本、分析结果，并批量写入技术栈条目"""
        jd = self.get_by_id(jd_id)
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

    def get_by_id(self, jd_id: int) -> JDModel | None:
        return self.db.query(JDModel).filter(JDModel.id == jd_id).first()

    def get_stack(self, jd_id: int) -> list[TechStackModel]:
        return (
            self.db.query(TechStackModel)
            .filter(TechStackModel.jd_id == jd_id)
            .order_by(TechStackModel.id)
            .all()
        )

    def list_recent(self, limit: int = 20, offset: int = 0) -> list[JDModel]:
        return (
            self.db.query(JDModel)
            .order_by(JDModel.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

"""evaluate 表的 DAO：评分记录的增查与聚合。"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from model.EvaluateModel import EvaluateModel


class EvaluateDAO:
    """费曼评分记录 DAO"""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        user_id: int,
        session_id: str,
        topic: str,
        rounds: int,
        score: float | None,
        summary: str,
        correct_points: str,
        wrong_points: str,
        missed_points: str,
        raw: str,
    ) -> EvaluateModel:
        row = EvaluateModel(
            user_id=user_id,
            session_id=session_id,
            topic=topic,
            rounds=rounds,
            score=score,
            summary=summary,
            correct_points=correct_points,
            wrong_points=wrong_points,
            missed_points=missed_points,
            raw=raw,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_by_id(self, evaluate_id: int, user_id: int) -> EvaluateModel | None:
        """按主键 + 属主查；越权查询（不是自己的记录）返回 None"""
        return (
            self.db.query(EvaluateModel)
            .filter(EvaluateModel.id == evaluate_id, EvaluateModel.user_id == user_id)
            .first()
        )

    def list_recent(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
        topic: str | None = None,
    ) -> list[EvaluateModel]:
        q = self.db.query(EvaluateModel).filter(EvaluateModel.user_id == user_id)
        if topic:
            q = q.filter(EvaluateModel.topic == topic)
        return q.order_by(EvaluateModel.id.desc()).offset(offset).limit(limit).all()

    def stats(self, user_id: int) -> dict:
        """当前用户统计：条数、平均分、按主题聚合"""
        total, avg_score = self.db.query(
            func.count(EvaluateModel.id),
            func.avg(EvaluateModel.score),
        ).filter(EvaluateModel.user_id == user_id).one()

        topic_rows = (
            self.db.query(
                EvaluateModel.topic,
                func.count(EvaluateModel.id),
                func.avg(EvaluateModel.score),
                func.max(EvaluateModel.created_at),
            )
            .filter(EvaluateModel.user_id == user_id)
            .group_by(EvaluateModel.topic)
            .order_by(func.max(EvaluateModel.created_at).desc())
            .all()
        )
        return {
            "total": total or 0,
            "avg_score": round(float(avg_score), 2) if avg_score is not None else None,
            "topics": [
                {
                    "topic": t,
                    "count": c,
                    "avg_score": round(float(a), 2) if a is not None else None,
                    "last_at": last.isoformat() if last else None,
                }
                for t, c, a, last in topic_rows
            ],
        }

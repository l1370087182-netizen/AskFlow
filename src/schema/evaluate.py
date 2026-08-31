import json
from datetime import datetime

from pydantic import BaseModel

from evaluate.rubric import level_of
from model.EvaluateModel import EvaluateModel


def _loads(text: str) -> list[str]:
    """JSON 数组字符串 → list，损坏时降级为空表"""
    try:
        data = json.loads(text)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


class EvaluateItem(BaseModel):
    """单条评分记录"""

    id: int
    session_id: str
    topic: str
    rounds: int
    score: float | None
    level: str
    summary: str
    correct_points: list[str]
    wrong_points: list[str]
    missed_points: list[str]
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: EvaluateModel) -> "EvaluateItem":
        return cls(
            id=row.id,
            session_id=row.session_id,
            topic=row.topic,
            rounds=row.rounds,
            score=row.score,
            level=level_of(row.score),
            summary=row.summary,
            correct_points=_loads(row.correct_points),
            wrong_points=_loads(row.wrong_points),
            missed_points=_loads(row.missed_points),
            created_at=row.created_at,
        )


class EvaluateListResponse(BaseModel):
    total: int
    items: list[EvaluateItem]


class TopicStat(BaseModel):
    topic: str
    count: int
    avg_score: float | None
    last_at: str | None


class EvaluateStats(BaseModel):
    """聚合统计：总条数、平均分、按主题汇总"""

    total: int
    avg_score: float | None
    topics: list[TopicStat]

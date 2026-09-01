"""知识点评估接口（阶段 9）：查询费曼模式的评分记录与统计。

评分记录由 /api/chat 的费曼模式评分时联动写入，这里只提供查询。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from DAO.evaluate_dao import EvaluateDAO
from auth.deps import get_current_user
from database.session import get_db
from model.UserModel import UserModel
from schema.evaluate import (
    EvaluateItem,
    EvaluateListResponse,
    EvaluateStats,
    TopicStat,
)

router = APIRouter(prefix="/api/evaluate", tags=["知识点评估"])


@router.get("/", response_model=EvaluateListResponse)
def list_evaluations(
    topic: str | None = Query(default=None, description="按主题过滤"),
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """评分记录列表（仅当前用户，最新在前）"""
    dao = EvaluateDAO(db)
    rows = dao.list_recent(user.id, limit=limit, offset=offset, topic=topic)
    return EvaluateListResponse(
        total=len(rows),
        items=[EvaluateItem.from_row(r) for r in rows],
    )


@router.get("/stats", response_model=EvaluateStats)
def evaluation_stats(
    db: Session = Depends(get_db), user: UserModel = Depends(get_current_user)
):
    """聚合统计（仅当前用户）：总条数、平均分、各主题掌握情况"""
    data = EvaluateDAO(db).stats(user.id)
    return EvaluateStats(
        total=data["total"],
        avg_score=data["avg_score"],
        topics=[TopicStat(**t) for t in data["topics"]],
    )


@router.get("/{evaluate_id}", response_model=EvaluateItem)
def get_evaluation(
    evaluate_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """单条评分详情（越权查询返回 404，不泄露存在性）"""
    row = EvaluateDAO(db).get_by_id(evaluate_id, user.id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到该评估记录")
    return EvaluateItem.from_row(row)

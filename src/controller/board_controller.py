"""学习任务板接口（阶段 2）：发布目标 / 查看任务树 / 取消。

任务板即 agent_task 表的用户视图：
- learning_goal  用户目标（自由文本 或 面试计划上板），planner 拆解为子题
- learning_item  单个学习子题，planner 基于知识库编学习材料
越权读路一律 404（隐藏存在性），与其他模块约定一致。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from DAO.agent_task_dao import AgentTaskDAO
from database.session import get_db
from model.AgentTaskModel import AgentTaskModel, TaskKind, TaskStatus
from model.InterviewRecordModel import InterviewRecordModel
from model.UserModel import UserModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/board", tags=["学习任务板"])

MAX_ACTIVE_GOALS = 5  # 单用户进行中（未完成未取消）目标上限


class GoalCreateRequest(BaseModel):
    """发布学习目标"""

    goal: str = Field(..., min_length=2, max_length=500, description="学习目标")


class FromInterviewRequest(BaseModel):
    """面试计划上板"""

    record_id: int = Field(..., description="面试记录 id")


def _item_view(t: AgentTaskModel) -> dict:
    out = t.output or {}
    payload = t.payload or {}
    return {
        "task_id": t.id,
        "topic": payload.get("topic", ""),
        "priority": payload.get("priority", "medium"),
        "reason": payload.get("reason", ""),
        "suggestion": payload.get("suggestion", ""),
        "status": t.status,
        "has_material": bool(out.get("material_md")),
        # 缺资料自动爬取：子题在等爬取任务终态（前端 pending 时显示「爬取资料中」）
        "waiting_crawl": bool(payload.get("crawl_task_id")),
    }


@router.post("/goals", status_code=202)
def create_goal(
    body: GoalCreateRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """发布学习目标：建 learning_goal 任务，planner 异步拆解成子题。"""
    dao = AgentTaskDAO(db)
    active = _count_active_goals(db, user.id)
    if active >= MAX_ACTIVE_GOALS:
        raise HTTPException(
            status_code=409,
            detail=f"进行中的学习目标最多 {MAX_ACTIVE_GOALS} 个，请先完成或取消",
        )
    task = dao.create(
        kind=TaskKind.LEARNING_GOAL,
        user_id=user.id,
        payload={"goal": body.goal.strip()},
        agent="api",
    )
    return {"task_id": task.id, "status": "pending"}


@router.post("/from-interview", status_code=202)
def from_interview(
    body: FromInterviewRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """面试计划上板：弱项+缺口转成学习子题（确定性拆解，不用模型）。"""
    rec = (
        db.query(InterviewRecordModel)
        .filter(InterviewRecordModel.id == body.record_id)
        .first()
    )
    if not rec or rec.user_id != user.id:
        raise HTTPException(status_code=404, detail="未找到该面试记录")

    dao = AgentTaskDAO(db)
    if _count_active_goals(db, user.id) >= MAX_ACTIVE_GOALS:
        raise HTTPException(
            status_code=409,
            detail=f"进行中的学习目标最多 {MAX_ACTIVE_GOALS} 个，请先完成或取消",
        )
    task = dao.create(
        kind=TaskKind.LEARNING_GOAL,
        user_id=user.id,
        payload={"interview_record_id": rec.id, "goal": f"面试补强 · {rec.jd_title or '模拟面试'}"},
        agent="api",
    )
    return {"task_id": task.id, "status": "pending"}


@router.get("/")
def list_board(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """任务板视图：目标（含子题进度），按时间倒序。"""
    goals = (
        db.query(AgentTaskModel)
        .filter(
            AgentTaskModel.user_id == user.id,
            AgentTaskModel.kind == TaskKind.LEARNING_GOAL,
        )
        .order_by(AgentTaskModel.id.desc())
        .limit(20)
        .all()
    )
    goal_ids = [g.id for g in goals]
    children: dict[str, list[AgentTaskModel]] = {gid: [] for gid in goal_ids}
    if goal_ids:
        rows = (
            db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.user_id == user.id,
                AgentTaskModel.kind == TaskKind.LEARNING_ITEM,
                AgentTaskModel.parent_id.in_(goal_ids),
            )
            .order_by(AgentTaskModel.id.asc())
            .all()
        )
        for r in rows:
            children.setdefault(r.parent_id, []).append(r)
    # 子题按拆解顺序展示（任务 id 是随机 hex，顺序存在 payload.order）
    for gid in children:
        children[gid].sort(key=lambda t: (t.payload or {}).get("order", 999))

    out = []
    for g in goals:
        items = [children.get(g.id, [])]
        item_rows = children.get(g.id, [])
        done = sum(1 for t in item_rows if t.status == TaskStatus.COMPLETED)
        out.append({
            "task_id": g.id,
            "goal": (g.payload or {}).get("goal", ""),
            "source": "interview" if (g.payload or {}).get("interview_record_id") else "goal",
            "status": g.status,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "progress": {"done": done, "total": len(item_rows)},
            "items": [_item_view(t) for t in item_rows],
        })
    return {"goals": out}


@router.get("/tasks/{task_id}")
def task_detail(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """任务详情（learning_item 看材料 / learning_goal 看拆解结果）。"""
    task = AgentTaskDAO(db).get(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="未找到该任务")
    return {
        "task_id": task.id,
        "kind": task.kind,
        "status": task.status,
        "payload": task.payload or {},
        "output": task.output or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """取消任务：目标是 learning_goal 时连带取消未完成的子题。"""
    dao = AgentTaskDAO(db)
    task = dao.get(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="未找到该任务")
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED):
        raise HTTPException(status_code=400, detail="任务已结束，无需取消")

    canceled = 0
    if task.kind == TaskKind.LEARNING_GOAL:
        # 连带取消未完成的子题
        rows = (
            db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.parent_id == task.id,
                AgentTaskModel.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
            )
            .all()
        )
        for r in rows:
            if dao.cancel_task(r.id):
                canceled += 1
    if dao.cancel_task(task.id):
        canceled += 1
    if canceled == 0:
        raise HTTPException(status_code=400, detail="任务已结束，无需取消")
    return {"canceled": canceled}


def _count_active_goals(db: Session, user_id: int) -> int:
    """用户进行中（pending/in_progress）的学习目标数"""
    return (
        db.query(AgentTaskModel)
        .filter(
            AgentTaskModel.user_id == user_id,
            AgentTaskModel.kind == TaskKind.LEARNING_GOAL,
            AgentTaskModel.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
        )
        .count()
    )

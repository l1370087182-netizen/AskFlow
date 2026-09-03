"""学习任务板接口（阶段 2）：发布目标 / 查看任务树 / 取消。

任务板即 agent_task 表的用户视图：
- learning_goal  用户目标（自由文本 或 面试计划上板），planner 拆解为子题
- learning_item  单个学习子题，planner 基于知识库编学习材料
越权读路一律 404（隐藏存在性），与其他模块约定一致。
"""
import json
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
from service.knowledge_service import (
    TASK_KEY,
    _redis,
    _synth_state,
    _view_by_db,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/board", tags=["学习任务板"])

MAX_ACTIVE_GOALS = 5  # 单用户进行中（未完成未取消）目标上限


class GoalCreateRequest(BaseModel):
    """发布学习目标"""

    goal: str = Field(..., min_length=2, max_length=500, description="学习目标")


class FromInterviewRequest(BaseModel):
    """面试计划上板"""

    record_id: int = Field(..., description="面试记录 id")


def _item_view(t: AgentTaskModel, crawl_progress: dict | None = None) -> dict:
    out = t.output or {}
    payload = t.payload or {}
    # 爬取链路仍活跃（检索中/排队/爬取中）才算等待——结束后子题即恢复可认领
    chain_active = bool(crawl_progress) and crawl_progress.get("status") in (
        "pending", "running", "searching"
    )
    return {
        "task_id": t.id,
        "topic": payload.get("topic", ""),
        "priority": payload.get("priority", "medium"),
        "reason": payload.get("reason", ""),
        "suggestion": payload.get("suggestion", ""),
        "status": t.status,
        "has_material": bool(out.get("material_md")),
        # 缺资料自动补爬：子题在等爬取链路（前端 pending 时显示进度条）
        "waiting_crawl": chain_active,
        "crawl_progress": crawl_progress if chain_active else None,
    }


def _crawl_progress_map(db: Session, ref_ids: list[str]) -> dict:
    """批量取子题引用的爬取/检索链路进度：{crawl_task_id: 进度视图}。

    两次 DB IN 查询 + 一次 Redis MGET。WEB_SEARCH 已完成时下钻一层看子
    crawl 的真实进度（子题真正等的是子任务跑完）；进度态缺失按 payload 合成。
    """
    if not ref_ids:
        return {}
    dao = AgentTaskDAO(db)
    rows = {r.id: r for r in dao.list_by_ids(ref_ids)}

    # WEB_SEARCH 已完成 → 跟到子 crawl（只跟一层）
    follow: dict[str, str] = {}
    for rid, row in rows.items():
        if row.kind == TaskKind.WEB_SEARCH and row.status == TaskStatus.COMPLETED:
            child = dao.find_child(rid, TaskKind.CRAWL)
            if child is not None:
                follow[rid] = child.id
    if follow:
        for r in dao.list_by_ids(list(follow.values())):
            rows[r.id] = r

    # Redis 进度一次 MGET（丢失的按 payload 合成，不影响状态判定）
    eff_ids = list(dict.fromkeys(
        follow.get(rid, rid) for rid in ref_ids if rid in rows
    ))
    states: dict[str, dict] = {}
    try:
        raws = _redis().mget([TASK_KEY.format(task_id=i) for i in eff_ids])
        for i, raw in zip(eff_ids, raws):
            if raw:
                try:
                    states[i] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
    except Exception as e:  # noqa: BLE001 —— Redis 不可用降级：只剩 DB 状态视图
        logger.warning("[board] 爬取进度批量查询降级（Redis 不可用）：%s", e)

    out: dict[str, dict] = {}
    for rid in ref_ids:
        if rid not in rows:
            continue  # 引用的任务行丢了（极端情况）：不展示进度条
        eff = rows[follow.get(rid, rid)]
        state = _view_by_db(eff, states.get(eff.id) or _synth_state(eff))
        out[rid] = {
            "kind": eff.kind,
            "status": state["status"],
            "done": state.get("done_pages", 0),
            "failed": state.get("failed_pages", 0),
            "skipped": state.get("skipped_pages", 0),
            "max_pages": state.get("max_pages", 0),
            "current_url": state.get("current_url", ""),
            "phase": state.get("phase", ""),
            "error": state.get("error", ""),
        }
    return out


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
    """任务板视图：目标（含子题进度），按创建时间倒序（新任务置顶）。

    注意：任务 id 是随机 hex，不能按 id 排序——必须 created_at。
    """
    goals = (
        db.query(AgentTaskModel)
        .filter(
            AgentTaskModel.user_id == user.id,
            AgentTaskModel.kind == TaskKind.LEARNING_GOAL,
        )
        .order_by(AgentTaskModel.created_at.desc(), AgentTaskModel.id.desc())
        .limit(100)
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

    # 批量取子题引用的爬取/检索链路进度（任务板进度条数据源）
    ref_ids = sorted({
        (t.payload or {}).get("crawl_task_id", "")
        for ts in children.values()
        for t in ts
        if (t.payload or {}).get("crawl_task_id")
    })
    progress_map = _crawl_progress_map(db, ref_ids)

    out = []
    for g in goals:
        item_rows = children.get(g.id, [])
        done = sum(1 for t in item_rows if t.status == TaskStatus.COMPLETED)
        out.append({
            "task_id": g.id,
            "goal": (g.payload or {}).get("goal", ""),
            "source": "interview" if (g.payload or {}).get("interview_record_id") else "goal",
            "status": g.status,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "progress": {"done": done, "total": len(item_rows)},
            "items": [
                _item_view(
                    t,
                    progress_map.get((t.payload or {}).get("crawl_task_id", "")),
                )
                for t in item_rows
            ],
        })
    return {"goals": out, "total": len(out)}


@router.get("/tasks/{task_id}")
def task_detail(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """任务详情（learning_item 看材料 / learning_goal 看拆解结果）。

    work_log 一并透出（work_log 查看器数据源；归属校验已挡住越权）。
    """
    task = AgentTaskDAO(db).get(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="未找到该任务")
    return {
        "task_id": task.id,
        "kind": task.kind,
        "status": task.status,
        "payload": task.payload or {},
        "output": task.output or {},
        "work_log": task.work_log or [],
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _cancel_crawl_chain(dao: AgentTaskDAO, ref_id: str) -> int:
    """顺着子题引用级联取消底层爬取链，返回取消条数。

    自动补爬任务不在目标子树（parent_id 不挂边），只在子题 payload 里留引用，
    必须显式顺藤摸瓜：CRAWL → 取消本身；WEB_SEARCH → 取消本身 + 子 crawl。
    """
    if not ref_id:
        return 0
    task = dao.get(ref_id)
    if task is None:
        return 0
    n = 1 if dao.cancel_task(ref_id) else 0
    if task.kind == TaskKind.WEB_SEARCH:
        child = dao.find_child(ref_id, TaskKind.CRAWL)
        if child is not None and dao.cancel_task(child.id):
            n += 1
    return n


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """取消任务：目标是 learning_goal 时连带取消未完成的子题，
    并级联取消每个子题引用的底层爬取/联网检索任务。

    已拆解完成（completed）的目标只要还有活跃子题也允许取消——
    只取消子题与爬取链，goal 行保持 completed；
    failed/canceled 或无任何可取消项才返回 400。
    """
    dao = AgentTaskDAO(db)
    task = dao.get(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="未找到该任务")
    if task.status in (TaskStatus.FAILED, TaskStatus.CANCELED):
        raise HTTPException(status_code=400, detail="任务已结束，无需取消")

    canceled = 0
    if task.kind == TaskKind.LEARNING_GOAL:
        # 连带取消未完成子题 + 各子题引用的爬取链
        rows = (
            db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.parent_id == task.id,
                AgentTaskModel.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
            )
            .all()
        )
        for r in rows:
            canceled += _cancel_crawl_chain(dao, (r.payload or {}).get("crawl_task_id", ""))
            if dao.cancel_task(r.id):
                canceled += 1
        # goal 本身仍活跃才取消；已拆解（completed）的只清子题不动 goal 行
        if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
            if dao.cancel_task(task.id):
                canceled += 1
    else:
        # learning_item / 爬取类任务：取消本身 + 顺引用级联
        if task.kind == TaskKind.LEARNING_ITEM:
            canceled += _cancel_crawl_chain(dao, (task.payload or {}).get("crawl_task_id", ""))
        elif task.kind == TaskKind.WEB_SEARCH:
            child = dao.find_child(task.id, TaskKind.CRAWL)
            if child is not None and dao.cancel_task(child.id):
                canceled += 1
        if dao.cancel_task(task.id):
            canceled += 1

    if canceled == 0:
        raise HTTPException(status_code=400, detail="任务已结束，无需取消")
    return {"canceled": canceled}


# ---------- 删除目标（整目标级联） ----------

def _collect_goal_tree(db: Session, goal_id: str) -> set[str]:
    """多源 BFS 收集目标关联的整棵树（应连带删除的任务行）。

    种子 = goal + 其子题 + 各子题引用的爬取/检索任务；
    再沿 parent_id 边向下到底：web_search → 子 crawl → quality_review → term_curate。
    一跳 parent_id 收不全质检链，必须递归。
    """
    ids: set[str] = {goal_id}
    queue: list[str] = []
    for it in (
        db.query(AgentTaskModel)
        .filter(AgentTaskModel.parent_id == goal_id)
        .all()
    ):
        if it.id not in ids:
            ids.add(it.id)
            queue.append(it.id)
        cid = (it.payload or {}).get("crawl_task_id", "")
        if cid and cid not in ids:
            ids.add(cid)
            queue.append(cid)
    while queue:
        pid = queue.pop()
        for c in (
            db.query(AgentTaskModel)
            .filter(AgentTaskModel.parent_id == pid)
            .all()
        ):
            if c.id not in ids:
                ids.add(c.id)
                queue.append(c.id)
    return ids


def _purge_tasks(db: Session, dao: AgentTaskDAO, ids: set[str], uid: int) -> tuple[int, int]:
    """先取消运行中的（停执行端），再删除任务行。返回 (删除数, 取消数)"""
    if not ids:
        return 0, 0
    rows = [r for r in dao.list_by_ids(list(ids)) if r.user_id == uid]
    canceled = 0
    for r in rows:
        if r.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
            if dao.cancel_task(r.id):
                canceled += 1
    for r in rows:
        db.delete(r)
    db.commit()
    return len(rows), canceled


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """删除整个学习目标：级联删除子题与整条爬取/检索/质检链 + 相关通知。

    - 运行中的任务先取消再删（执行侧探针会尽快停下）
    - 已爬入知识库的内容保留（与取消语义一致）
    - 删除后再补一次同条件清扫：压掉「planner 并发建子题」的孤儿窗口
    - goal 行任何状态可删；非 goal 任务不允许从这里删
    """
    dao = AgentTaskDAO(db)
    goal = dao.get(task_id)
    if not goal or goal.user_id != user.id:
        raise HTTPException(status_code=404, detail="未找到该任务")
    if goal.kind != TaskKind.LEARNING_GOAL:
        raise HTTPException(status_code=400, detail="仅支持删除学习目标（子题随目标整体删除）")

    ids = _collect_goal_tree(db, task_id)
    deleted, canceled = _purge_tasks(db, dao, ids, user.id)

    # 二次清扫：收集与删除之间 planner 可能刚建好的子题（残余窗口书面接受）
    leftovers = _collect_goal_tree(db, task_id) - ids
    if leftovers:
        d2, c2 = _purge_tasks(db, dao, leftovers, user.id)
        deleted += d2
        canceled += c2
        ids |= leftovers

    # 相关通知清理（费曼/面试通知的 ref_id 是会话/记录 id，不受影响）
    try:
        from DAO.notification_dao import NotificationDAO

        NotificationDAO(db).delete_by_refs(user.id, list(ids))
    except Exception:  # noqa: BLE001 —— 通知清理失败不阻断删除
        logger.warning("[board] 删除目标时通知清理失败", exc_info=True)

    # Redis 进度键尽力清理（执行端可能补写终态，残留无害：无读路再触达）
    try:
        from service.knowledge_service import TASK_KEY, _redis

        r = _redis()
        for tid in ids:
            r.delete(TASK_KEY.format(task_id=tid))
    except Exception:  # noqa: BLE001
        pass

    return {"deleted": deleted, "canceled": canceled}


@router.get("/agents")
def agents_status(
    user: UserModel = Depends(get_current_user),
):
    """各 agent 实时状态（任务板活动面板）；他人任务细节由 manager 屏蔽。"""
    from agent_engine.manager import agent_statuses

    return {"agents": agent_statuses(user.id)}


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

"""站内通知接口：列表 / 未读数 / 批量已读 / 批量删除。

全部按 user_id 过滤（越权操作只影响本人数据，天然隔离）。
写入端不在这里——通知由任务引擎终态挂钩与评分/面试落库点产生。
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from DAO.notification_dao import NotificationDAO
from database.session import get_db
from model.UserModel import UserModel
from schema.notification import (
    NotificationBatchRequest,
    NotificationItem,
    NotificationListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notification", tags=["通知"])


@router.get("/", response_model=NotificationListResponse)
def list_notifications(
    limit: int = Query(default=30, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """通知列表（id 倒序）+ 未读数（面板打开时拉取）"""
    dao = NotificationDAO(db)
    rows = dao.list_recent(user.id, limit=limit, offset=offset)
    return NotificationListResponse(
        total=dao.count(user.id),
        unread_count=dao.count_unread(user.id),
        items=[NotificationItem.from_row(r) for r in rows],
    )


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """轻量未读数（铃铛红点轮询专用，只走一条 COUNT）"""
    return {"unread": NotificationDAO(db).count_unread(user.id)}


@router.post("/read")
def mark_read(
    body: NotificationBatchRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """批量已读：ids 精确；all=true 全量；都不传视为全量"""
    ids = None if (body.all or body.ids is None) else body.ids
    n = NotificationDAO(db).mark_read(user.id, ids)
    return {"marked": n}


@router.post("/delete")
def delete_notifications(
    body: NotificationBatchRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """批量删除：ids 精确；all=true 全量；都不传视为全量"""
    ids = None if (body.all or body.ids is None) else body.ids
    n = NotificationDAO(db).delete(user.id, ids)
    return {"deleted": n}

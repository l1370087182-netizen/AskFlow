from pydantic import BaseModel, Field
from datetime import datetime

from model.NotificationModel import NotificationModel


class NotificationItem(BaseModel):
    """单条通知"""

    id: int
    type: str
    title: str
    body: str
    link: str
    ref_id: str = ""
    is_read: bool
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: NotificationModel) -> "NotificationItem":
        return cls(
            id=row.id,
            type=row.type,
            title=row.title,
            body=row.body,
            link=row.link,
            ref_id=row.ref_id,
            is_read=row.is_read,
            created_at=row.created_at,
        )


class NotificationListResponse(BaseModel):
    total: int
    unread_count: int
    items: list[NotificationItem]


class NotificationBatchRequest(BaseModel):
    """批量已读/删除：传 ids 精确操作，或 all=true 全量（二者取其一，默认全量）"""

    ids: list[int] | None = None
    all: bool = False

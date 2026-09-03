"""NotificationDAO：站内通知存储（写入/列表/已读/删除/裁旧）。

查询风格与 knowledge_dao 一致：user_id 过滤 + id 倒序 + limit/offset。
"""
from sqlalchemy.orm import Session

from model.NotificationModel import NotificationModel

# 每用户保留条数上限（插新裁旧，防无限膨胀）
KEEP_PER_USER = 200


class NotificationDAO:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        user_id: int,
        type: str,
        title: str,
        body: str = "",
        link: str = "",
        ref_id: str = "",
    ) -> NotificationModel:
        """写一条通知并裁旧（保留最近 KEEP_PER_USER 条）"""
        row = NotificationModel(
            user_id=user_id,
            type=type,
            title=title[:200],
            body=(body or "")[:500],
            link=(link or "")[:300],
            ref_id=str(ref_id)[:64],
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        self._prune(user_id)
        return row

    def _prune(self, user_id: int) -> None:
        """超出保留上限时删除最旧的（容忍并发短暂超 1~2 条）"""
        rows = (
            self.db.query(NotificationModel.id)
            .filter(NotificationModel.user_id == user_id)
            .order_by(NotificationModel.id.desc())
            .offset(KEEP_PER_USER)
            .all()
        )
        old_ids = [r.id for r in rows]
        if old_ids:
            (
                self.db.query(NotificationModel)
                .filter(NotificationModel.id.in_(old_ids))
                .delete(synchronize_session=False)
            )
            self.db.commit()

    def list_recent(
        self, user_id: int, limit: int = 30, offset: int = 0
    ) -> list[NotificationModel]:
        return (
            self.db.query(NotificationModel)
            .filter(NotificationModel.user_id == user_id)
            .order_by(NotificationModel.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def count(self, user_id: int) -> int:
        return (
            self.db.query(NotificationModel)
            .filter(NotificationModel.user_id == user_id)
            .count()
        )

    def count_unread(self, user_id: int) -> int:
        return (
            self.db.query(NotificationModel)
            .filter(
                NotificationModel.user_id == user_id,
                NotificationModel.is_read.is_(False),
            )
            .count()
        )

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int:
        """标记已读；ids=None 表示全部。返回受影响条数（只操作本人通知）"""
        q = self.db.query(NotificationModel).filter(
            NotificationModel.user_id == user_id,
            NotificationModel.is_read.is_(False),
        )
        if ids is not None:
            if not ids:
                return 0
            q = q.filter(NotificationModel.id.in_(ids))
        n = q.update(
            {NotificationModel.is_read: True}, synchronize_session=False
        )
        self.db.commit()
        return n

    def delete(self, user_id: int, ids: list[int] | None = None) -> int:
        """删除通知；ids=None 表示全部。返回受影响条数（只操作本人通知）"""
        q = self.db.query(NotificationModel).filter(
            NotificationModel.user_id == user_id
        )
        if ids is not None:
            if not ids:
                return 0
            q = q.filter(NotificationModel.id.in_(ids))
        n = q.delete(synchronize_session=False)
        self.db.commit()
        return n

    def delete_by_refs(self, user_id: int, ref_ids: list[str]) -> int:
        """按关联对象 id 批量清理（删除目标任务时连带清通知）"""
        if not ref_ids:
            return 0
        n = (
            self.db.query(NotificationModel)
            .filter(
                NotificationModel.user_id == user_id,
                NotificationModel.ref_id.in_([str(r) for r in ref_ids]),
            )
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return n

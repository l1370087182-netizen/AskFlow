"""notification 表：站内通知（任务终态 / 费曼评分 / 面试总评）。

- 由任务引擎终态挂钩（agent_task_dao.write_back/fail_task）与
  对话评分、面试总评落库点写入；前端铃铛轮询消费
- ref_id：关联对象 id（任务 id / 会话 id / 面试记录 id），删除目标时按
  ref_id 批量清理；费曼/面试通知的 ref_id 不是任务 id，不受任务删除影响
- 每用户保留上限由 NotificationDAO.prune 控制（插新裁旧）
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class NotificationModel(Base):
    """站内通知"""

    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_user_read", "user_id", "is_read"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键（增量游标）"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="接收用户"
    )
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="task",
        comment="事件类型：task_done/task_failed/evaluation/interview",
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="通知标题"
    )
    body: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="正文摘要"
    )
    link: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", comment="点击跳转的前端深链接"
    )
    ref_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
        comment="关联对象 id（任务/会话/记录），用于批量清理",
    )
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已读"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )

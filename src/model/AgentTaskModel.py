"""任务引擎：统一任务表（黑板精简版，设计见 docs/RAG×多Agent融合升级方案.md）。

所有后台异步任务（爬取 / 质检 / 术语整理 / 学习规划……）统一登记在本表：
- 数据库是唯一真相源（AGENT 项目铁律 9），Redis 只做认领锁与任务内进度缓存
- status 状态机白名单在存储层强制（AgentTaskDAO 拒绝非法流转）
- version 乐观锁 CAS：认领/写回都带版本条件，冲突即放弃（幂等）
- work_log 强制留痕：无日志的写回被存储层拒绝
- heartbeat_at：执行期心跳，超时回收的依据
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class TaskStatus:
    """任务状态"""

    PENDING = "pending"          # 待认领
    IN_PROGRESS = "in_progress"  # 已认领，执行中
    COMPLETED = "completed"      # 成功
    FAILED = "failed"            # 重试耗尽 / 永久性错误
    CANCELED = "canceled"        # 用户主动取消（任务板）


# 流转白名单：其余流转一律被存储层拒绝（IllegalTransitionError）
ALLOWED_TRANSITIONS = {
    (TaskStatus.PENDING, TaskStatus.IN_PROGRESS),      # 认领（CAS）
    (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED),    # 成功写回
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED),       # 重试耗尽 / 永久错误
    (TaskStatus.IN_PROGRESS, TaskStatus.PENDING),      # 超时回收 / 主动退让重试
    (TaskStatus.PENDING, TaskStatus.CANCELED),         # 用户取消（任务板）
    (TaskStatus.IN_PROGRESS, TaskStatus.CANCELED),     # 用户取消执行中任务
}


class TaskKind:
    """任务类型注册表：每种 kind 对应一个 Agent 角色"""

    CRAWL = "crawl"                    # 整站浅爬（producer，已接入）
    QUALITY_REVIEW = "quality_review"  # 内容质检（reviewer）
    TERM_CURATE = "term_curate"        # 术语整理（curator）
    STUDY_PLAN = "study_plan"          # 面试记录 → 学习计划（planner）
    LEARNING_GOAL = "learning_goal"   # 任务板：用户学习目标（planner 拆解）
    LEARNING_ITEM = "learning_item"   # 任务板：单个学习子题（planner 编材料）


class IllegalTransitionError(ValueError):
    """非法状态流转（状态机的存储层把关）"""


class AgentTaskModel(Base):
    __tablename__ = "agent_task"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, comment="任务 id（token_hex(12)）"
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="任务类型，见 TaskKind"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.PENDING, index=True,
        comment="状态，见 TaskStatus",
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True,
        comment="归属用户；0=系统/全局",
    )
    trace_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", index=True,
        comment="全链路追踪 id（同一业务链路共享）",
    )
    assignee: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="认领者 agent_id"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="CAS 乐观锁版本号"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已重试次数"
    )
    tags: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="标签（路由预留）"
    )
    payload: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, comment="任务输入"
    )
    output: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, comment="任务结果"
    )
    work_log: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list,
        comment="工作日志（强制：每条含 agent/action/description/fields/ts）",
    )
    parent_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="父任务 id（子任务派发边）"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="认领时间"
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近执行心跳（超时回收依据）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now,
        comment="更新时间",
    )

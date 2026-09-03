"""任务引擎存储层：AgentTaskDAO（黑板精简版的存储把关者）。

落实设计文档 §六 的机制：
- 状态机白名单：非法流转直接抛 IllegalTransitionError
- 两层锁的 DB 层 = CAS：认领/写回都带 version（+assignee）条件，
  rowcount != 1 即冲突，调用方立即放弃（幂等，迟到写回不会覆盖）
- work_log 强制：认领/写回/回收/失败都必须附日志条目
- 超时回收：find_stale / reclaim_stale / fail_task
"""
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from model.AgentTaskModel import (
    ALLOWED_TRANSITIONS,
    AgentTaskModel,
    IllegalTransitionError,
    TaskStatus,
)


def _log_entry(agent: str, action: str, description: str = "", fields: list | None = None) -> dict:
    """标准 work_log 条目（强制格式，见模型注释）"""
    return {
        "agent": agent,
        "action": action,
        "description": description,
        "fields": fields or [],
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


class AgentTaskDAO:
    def __init__(self, db: Session):
        self.db = db

    # ---------- 创建 / 查询 ----------

    def create(
        self,
        *,
        kind: str,
        user_id: int = 0,
        payload: dict | None = None,
        tags: list | None = None,
        parent_id: str = "",
        trace_id: str = "",
        agent: str = "system",
    ) -> AgentTaskModel:
        """创建任务（一律 pending），自动写首条 work_log"""
        row = AgentTaskModel(
            id=secrets.token_hex(12),
            kind=kind,
            status=TaskStatus.PENDING,
            user_id=user_id,
            trace_id=trace_id or secrets.token_hex(8),
            payload=payload or {},
            tags=tags or [],
            parent_id=parent_id,
            work_log=[_log_entry(agent, "create", "任务已创建")],
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get(self, task_id: str) -> AgentTaskModel | None:
        return self.db.get(AgentTaskModel, task_id)

    def list_claimable(self, kinds: list[str], limit: int = 8) -> list[AgentTaskModel]:
        """可认领任务（pending），按创建时间先来先出"""
        return (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.kind.in_(kinds),
                AgentTaskModel.status == TaskStatus.PENDING,
            )
            .order_by(AgentTaskModel.created_at.asc(), AgentTaskModel.id.asc())
            .limit(limit)
            .all()
        )

    def list_by_user(
        self,
        user_id: int,
        kind: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[AgentTaskModel]:
        """按归属用户查任务（活跃列表/上限统计用）"""
        q = self.db.query(AgentTaskModel).filter(AgentTaskModel.user_id == user_id)
        if kind:
            q = q.filter(AgentTaskModel.kind == kind)
        if statuses:
            q = q.filter(AgentTaskModel.status.in_(statuses))
        return q.order_by(AgentTaskModel.created_at.desc()).limit(limit).all()

    def count_active(self, user_id: int, kind: str) -> int:
        """用户活跃（待认领+执行中）任务数，提交上限用"""
        return (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.user_id == user_id,
                AgentTaskModel.kind == kind,
                AgentTaskModel.status.in_(
                    [TaskStatus.PENDING, TaskStatus.IN_PROGRESS]
                ),
            )
            .count()
        )

    # ---------- 状态机把关 ----------

    @staticmethod
    def _assert_transition(old: str, new: str) -> None:
        if (old, new) not in ALLOWED_TRANSITIONS:
            raise IllegalTransitionError(f"非法任务状态流转：{old} → {new}")

    # ---------- 认领（两层锁之 CAS 层） ----------

    def claim_cas(
        self, task_id: str, agent_id: str, expected_version: int
    ) -> AgentTaskModel | None:
        """认领：仅当仍 pending 且版本一致才成功；冲突返回 None，调用方立即放弃"""
        row = self.get(task_id)
        if row is None or row.status != TaskStatus.PENDING:
            return None
        self._assert_transition(TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
        now = datetime.now()
        n = (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.id == task_id,
                AgentTaskModel.status == TaskStatus.PENDING,
                AgentTaskModel.version == expected_version,
            )
            .update(
                {
                    AgentTaskModel.status: TaskStatus.IN_PROGRESS,
                    AgentTaskModel.assignee: agent_id,
                    AgentTaskModel.claimed_at: now,
                    AgentTaskModel.heartbeat_at: now,
                    AgentTaskModel.version: expected_version + 1,
                },
                synchronize_session=False,
            )
        )
        if n != 1:
            self.db.rollback()
            return None
        self.db.expire_all()  # 批量 UPDATE 不过会话缓存，先过期再取最新行
        row = self.get(task_id)
        row.work_log = (row.work_log or []) + [_log_entry(agent_id, "claim", "认领任务")]
        flag_modified(row, "work_log")
        self.db.commit()
        self.db.refresh(row)
        return row

    # ---------- 写回（幂等 CAS） ----------

    def write_back(
        self,
        task_id: str,
        agent_id: str,
        expected_version: int,
        *,
        status: str = TaskStatus.COMPLETED,
        output: dict | None = None,
        log_action: str = "complete",
        log_desc: str = "",
    ) -> AgentTaskModel | None:
        """成功/失败写回：id+version+assignee 全对上才写入，否则 None（冲突放弃）。

        防的是「超时回收后原处理者迟到的结果覆盖新处理者」——铁律 4。
        """
        row = self.get(task_id)
        if row is None or row.assignee != agent_id:
            return None
        self._assert_transition(row.status, status)
        n = (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.id == task_id,
                AgentTaskModel.status == TaskStatus.IN_PROGRESS,
                AgentTaskModel.version == expected_version,
                AgentTaskModel.assignee == agent_id,
            )
            .update(
                {
                    AgentTaskModel.status: status,
                    AgentTaskModel.output: output or {},
                    AgentTaskModel.version: expected_version + 1,
                    AgentTaskModel.heartbeat_at: datetime.now(),
                },
                synchronize_session=False,
            )
        )
        if n != 1:
            self.db.rollback()
            return None
        self.db.expire_all()
        row = self.get(task_id)
        row.work_log = (row.work_log or []) + [_log_entry(agent_id, log_action, log_desc)]
        flag_modified(row, "work_log")
        self.db.commit()
        self.db.refresh(row)
        self._notify_terminal(row, status)
        return row

    def heartbeat(self, task_id: str, agent_id: str) -> bool:
        """执行期心跳：只刷时间不动 version（写回 CAS 用的还是认领后的版本）。

        返回 False = 命中 0 行，任务已不在「in_progress + 本人认领」状态，
        有三种可能：被用户取消 / 被 reaper 回收回 pending / 被 reaper 判败。
        调用方应回读真实状态（get）定性并终止执行——它是执行侧唯一的
        取消/中断探针（心跳兼作取消检查点）。
        """
        n = (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.id == task_id,
                AgentTaskModel.assignee == agent_id,
                AgentTaskModel.status == TaskStatus.IN_PROGRESS,
            )
            .update({AgentTaskModel.heartbeat_at: datetime.now()}, synchronize_session=False)
        )
        self.db.commit()
        return n == 1

    # ---------- 退让 / 失败 / 超时回收 ----------

    def release_for_retry(
        self, task_id: str, agent_id: str, expected_version: int, reason: str = ""
    ) -> AgentTaskModel | None:
        """执行异常退让：回 pending 等再次认领（重试次数 +1），带日志"""
        return self._back_to_pending(
            task_id, agent_id, expected_version,
            reason=f"执行异常退让：{reason}", retry_bump=True,
        )

    def reclaim_stale(
        self, task_id: str, reaper_id: str, expected_version: int
    ) -> AgentTaskModel | None:
        """超时回收：in_progress 心跳超时 → 回 pending（重试次数 +1）"""
        return self._back_to_pending(
            task_id, reaper_id, expected_version,
            reason="心跳超时，回收待重新认领", retry_bump=True,
        )

    def _back_to_pending(
        self,
        task_id: str,
        agent_id: str,
        expected_version: int,
        *,
        reason: str,
        retry_bump: bool,
    ) -> AgentTaskModel | None:
        row = self.get(task_id)
        if row is None:
            return None
        self._assert_transition(row.status, TaskStatus.PENDING)
        values = {
            AgentTaskModel.status: TaskStatus.PENDING,
            AgentTaskModel.assignee: "",
            AgentTaskModel.claimed_at: None,
            AgentTaskModel.heartbeat_at: None,
            AgentTaskModel.version: expected_version + 1,
        }
        if retry_bump:
            values[AgentTaskModel.retry_count] = row.retry_count + 1
        n = (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.id == task_id,
                AgentTaskModel.status == TaskStatus.IN_PROGRESS,
                AgentTaskModel.version == expected_version,
            )
            .update(values, synchronize_session=False)
        )
        if n != 1:
            self.db.rollback()
            return None
        self.db.expire_all()
        row = self.get(task_id)
        row.work_log = (row.work_log or []) + [_log_entry(agent_id, "retry", reason)]
        flag_modified(row, "work_log")
        self.db.commit()
        self.db.refresh(row)
        return row

    def fail_task(
        self, task_id: str, agent_id: str, expected_version: int, reason: str = ""
    ) -> AgentTaskModel | None:
        """标记失败（重试耗尽 / 永久性错误），原因写入 output.error 与日志"""
        row = self.get(task_id)
        if row is None:
            return None
        self._assert_transition(row.status, TaskStatus.FAILED)
        n = (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.id == task_id,
                AgentTaskModel.status == TaskStatus.IN_PROGRESS,
                AgentTaskModel.version == expected_version,
            )
            .update(
                {
                    AgentTaskModel.status: TaskStatus.FAILED,
                    AgentTaskModel.output: {"error": reason},
                    AgentTaskModel.version: expected_version + 1,
                },
                synchronize_session=False,
            )
        )
        if n != 1:
            self.db.rollback()
            return None
        self.db.expire_all()
        row = self.get(task_id)
        row.work_log = (row.work_log or []) + [_log_entry(agent_id, "fail", reason)]
        flag_modified(row, "work_log")
        self.db.commit()
        self.db.refresh(row)
        self._notify_terminal(row, TaskStatus.FAILED)
        return row

    def cancel_task(self, task_id: str, operator: str = "user") -> AgentTaskModel | None:
        """用户取消（任务板）：pending/in_progress → canceled。

        CAS 化（与认领同构）：原子 UPDATE 带状态条件，并发下不会把已终态
        的任务覆盖成 canceled，也不会「读到 pending 取消、却被并发认领抢跑」；
        成功后 version+1——先于取消认领的执行者迟到写回会被版本条件拒绝，
        「取消没生效」换姿势复现的路一并堵死。已终态/不存在返回 None。
        """
        n = (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.id == task_id,
                AgentTaskModel.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
            )
            .update(
                {
                    AgentTaskModel.status: TaskStatus.CANCELED,
                    AgentTaskModel.output: {"canceled": True},
                    AgentTaskModel.version: AgentTaskModel.version + 1,
                },
                synchronize_session=False,
            )
        )
        if n != 1:
            self.db.rollback()
            return None
        self.db.expire_all()
        row = self.get(task_id)
        row.work_log = (row.work_log or []) + [_log_entry(operator, "cancel", "用户取消")]
        flag_modified(row, "work_log")
        self.db.commit()
        self.db.refresh(row)
        return row

    # ---------- 批量 / 关系查询 ----------

    def list_by_ids(self, task_ids: list[str]) -> list[AgentTaskModel]:
        """按 id 批量取任务（任务板批量进度用）"""
        if not task_ids:
            return []
        return (
            self.db.query(AgentTaskModel)
            .filter(AgentTaskModel.id.in_(task_ids))
            .all()
        )

    def find_child(self, parent_id: str, kind: str) -> AgentTaskModel | None:
        """反查父任务下指定类型的子任务（如 WEB_SEARCH → 子 crawl；
        取最早一条——子任务创建是单发语义，正常只有一条）"""
        if not parent_id:
            return None
        return (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.parent_id == parent_id,
                AgentTaskModel.kind == kind,
            )
            .order_by(AgentTaskModel.created_at.asc())
            .first()
        )

    # ---------- 超时扫描 ----------

    @staticmethod
    def _notify_terminal(row: "AgentTaskModel", status: str) -> None:
        """终态通知挂钩：尽力而为，绝不影响任务状态流转。

        传纯数据快照给通知服务（其内部用独立 Session 写入）——
        避免通知写入异常污染当前会话（producer 写回后紧接建质检子任务）。
        延迟 import 防模块级循环依赖。
        """
        try:
            from service.notification_service import notify_task_terminal

            notify_task_terminal({
                "id": row.id,
                "kind": row.kind,
                "user_id": row.user_id,
                "status": status,
                "assignee": row.assignee,
                "parent_id": row.parent_id,
                "payload": dict(row.payload or {}),
                "output": dict(row.output or {}),
            })
        except Exception:  # noqa: BLE001 —— 通知失败静默，状态流转不受影响
            pass

    def find_stale(self, timeout_seconds: int) -> list[AgentTaskModel]:
        """心跳超时的执行中任务（回收器用）"""
        deadline = datetime.now() - timedelta(seconds=timeout_seconds)
        return (
            self.db.query(AgentTaskModel)
            .filter(
                AgentTaskModel.status == TaskStatus.IN_PROGRESS,
                AgentTaskModel.heartbeat_at < deadline,
            )
            .all()
        )

    def find_in_progress(self) -> list[AgentTaskModel]:
        """全部执行中任务（启动自检用：单进程部署下，重启即意味着
        所有 in_progress 的执行线程已随上个进程死亡，全部是遗留任务）"""
        return (
            self.db.query(AgentTaskModel)
            .filter(AgentTaskModel.status == TaskStatus.IN_PROGRESS)
            .all()
        )

"""超时回收器：唯一的集中式兜底调度（AGENT 项目 TimeoutReaper 的同步版）。

定期扫描心跳超时的执行中任务：
- 重试未耗尽 → 回收为 pending（retry+1），等别的 Agent 重新认领
- 重试耗尽   → failed，跳过不阻塞下游（铁律：流水线不挂死）
"""
import logging
import threading

from DAO.agent_task_dao import AgentTaskDAO
from database.session import SessionLocal
from agent_engine.base_agent import MAX_RETRY

logger = logging.getLogger(__name__)

TASK_TIMEOUT_SEC = 300      # 心跳超时判定（5 分钟）
REAPER_INTERVAL_SEC = 10    # 扫描间隔
REAPER_ID = "reaper-01"


class TimeoutReaper(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="agent-reaper")
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        logger.info(
            "[reaper] 启动（心跳超时 %ss，每 %ss 扫一轮）",
            TASK_TIMEOUT_SEC, REAPER_INTERVAL_SEC,
        )
        while not self._stopped.is_set():
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001 —— 兜底线程不能死
                logger.exception("[reaper] 扫描异常")
            self._stopped.wait(REAPER_INTERVAL_SEC)

    def reap_once(self) -> None:
        """扫一轮：超时任务按重试次数分流"""
        db = SessionLocal()
        try:
            dao = AgentTaskDAO(db)
            for task in dao.find_stale(TASK_TIMEOUT_SEC):
                if task.retry_count >= MAX_RETRY:
                    dao.fail_task(
                        task.id, REAPER_ID, task.version,
                        reason="心跳超时且重试耗尽",
                    )
                    logger.warning("[reaper] 任务 %s 重试耗尽，标记失败", task.id)
                else:
                    dao.reclaim_stale(task.id, REAPER_ID, task.version)
                    logger.warning(
                        "[reaper] 任务 %s 心跳超时，回收重试（第 %s 次）",
                        task.id, task.retry_count + 1,
                    )
        finally:
            db.close()

    @staticmethod
    def startup_sweep() -> None:
        """启动自检：上次停机/崩溃遗留的执行中任务立即回收。

        执行线程在本进程内（单进程部署），进程一死全部 in_progress 都是遗留，
        不看心跳阈值直接回收，不等 5 分钟超时。
        """
        db = SessionLocal()
        try:
            dao = AgentTaskDAO(db)
            leftover = dao.find_in_progress()
            for task in leftover:
                if task.retry_count >= MAX_RETRY:
                    dao.fail_task(
                        task.id, REAPER_ID, task.version, reason="启动自检：重试耗尽"
                    )
                else:
                    dao.reclaim_stale(task.id, REAPER_ID, task.version)
            if leftover:
                logger.warning("[reaper] 启动自检：回收遗留任务 %s 个", len(leftover))
        except Exception:  # noqa: BLE001 —— 自检失败不影响启动
            logger.exception("[reaper] 启动自检失败")
        finally:
            db.close()

"""BaseAgent：常驻 Agent 的统一骨架（同步线程版，与本项目技术栈一致）。

每个 Agent 一条 daemon 线程，独立循环：
    巡检可认领任务（自己负责的 kind）→ 两层锁认领 → 执行 → 幂等写回
子类只需声明 KINDS 并实现 process(task, db)。

没有中央调度器：谁干什么由「kind 归属 + 先来先出 + 认领竞争」涌现。

活动状态（任务板可视化）：`self.activity` 供 manager.agent_statuses 读取。
铁律：永远整体替换（`self.activity = {...}` 或 `{**self.activity, ...}`），
禁止原地改字段——CPython 下引用替换是原子的，读线程不会读到撕裂状态。
"""
import logging
import random
import threading
import time

from DAO.agent_task_dao import AgentTaskDAO
from database.session import SessionLocal
from model.AgentTaskModel import AgentTaskModel, TaskStatus
from agent_engine.locks import ClaimLock

logger = logging.getLogger(__name__)

MAX_RETRY = 3               # 重试上限：耗尽转 failed，不挂死
CLAIM_LOCK = ClaimLock()    # 全 Agent 共享的认领锁实例

_IDLE = lambda: {"status": "idle", "task_id": "", "kind": "", "desc": "", "since": time.time()}  # noqa: E731


class TaskPermanentError(Exception):
    """永久性错误：重试也不会好（缺配置/参数非法），直接 fail"""


class BaseAgent(threading.Thread):
    """Agent 基类：子类实现 process()，声明 KINDS"""

    KINDS: list[str] = []      # 负责任务类型
    POLL_INTERVAL = 3.0        # 巡检间隔（秒），轮询兜底

    def __init__(self, agent_id: str):
        super().__init__(daemon=True, name=agent_id)
        self.agent_id = agent_id
        self._stopped = threading.Event()
        # 活动快照（展示用，弱一致可接受；读写规则见模块 docstring）
        self.activity = _IDLE()

    def stop(self) -> None:
        self._stopped.set()

    def _note(self, desc: str) -> None:
        """更新当前阶段描述（子类在 process 内调用；整体替换保证原子可见）"""
        self.activity = {**self.activity, "desc": desc}

    @staticmethod
    def _activity_desc(task: AgentTaskModel) -> str:
        """认领时的初始描述：kind + payload 里的主题/目标/URL 摘要"""
        p = task.payload or {}
        subject = str(p.get("topic") or p.get("goal") or p.get("url") or "").strip()[:30]
        return f"处理 {task.kind} 任务" + (f"：{subject}" if subject else "")

    # ---------- 主循环 ----------

    def run(self) -> None:
        logger.info("[agent:%s] 启动，负责 kinds=%s", self.agent_id, self.KINDS)
        while not self._stopped.is_set():
            try:
                self.process_once()
            except Exception:  # noqa: BLE001 —— 本轮巡检异常吞掉，线程不能死
                logger.exception("[agent:%s] 巡检异常", self.agent_id)
            # 抖动错峰：多实例不同步轮询，也避免 LLM 调用齐发
            self._stopped.wait(self.POLL_INTERVAL + random.uniform(0, 1.0))
        logger.info("[agent:%s] 已停止", self.agent_id)

    def process_once(self) -> None:
        """一轮：查可认领任务 → 逐个尝试认领并执行"""
        db = SessionLocal()
        try:
            dao = AgentTaskDAO(db)
            for task in dao.list_claimable(self.KINDS):
                if self._stopped.is_set():
                    return
                if not self._should_claim(dao, task):
                    continue  # 子类判定暂不认领（如 learning_item 等爬取完成）
                self._try_process(dao, task, db)
        finally:
            db.close()

    def _should_claim(self, dao, task) -> bool:  # noqa: ANN001 —— 钩子无需精确类型
        """认领前过滤钩子：默认都可认领；子类可按任务 payload 延后认领。

        返回 False 的任务本轮跳过、保持 pending，下轮巡检再看——
        用于「等外部任务终态再执行」的场景（不消耗重试次数、不写日志）。
        """
        return True

    def _try_process(self, dao: AgentTaskDAO, task: AgentTaskModel, db) -> None:
        """两层锁认领 + 执行 + 失败分流"""
        if not CLAIM_LOCK.acquire(task.id, self.agent_id):
            return  # 别的实例正在处理
        claimed = None
        try:
            claimed = dao.claim_cas(task.id, self.agent_id, task.version)
            if claimed is None:
                return  # CAS 冲突/已被抢走，立即放弃
            self.activity = {
                "status": "working",
                "task_id": claimed.id,
                "kind": claimed.kind,
                "desc": self._activity_desc(claimed),
                "since": time.time(),
            }
            try:
                self.process(claimed, db)
            except TaskPermanentError as e:
                dao.fail_task(claimed.id, self.agent_id, claimed.version, reason=str(e))
                logger.warning("[agent:%s] 任务 %s 永久失败：%s", self.agent_id, task.id, e)
            except Exception as e:  # noqa: BLE001 —— 偶发异常走重试分流
                logger.exception("[agent:%s] 任务 %s 执行异常", self.agent_id, task.id)
                self._handle_transient_failure(dao, claimed, e)
        finally:
            CLAIM_LOCK.release(task.id, self.agent_id)
            self.activity = _IDLE()

    def _handle_transient_failure(
        self, dao: AgentTaskDAO, task: AgentTaskModel, err: Exception
    ) -> None:
        """偶发失败：未耗尽 → 退回 pending 等再认领；耗尽 → fail"""
        if task.retry_count >= MAX_RETRY:
            dao.fail_task(
                task.id, self.agent_id, task.version,
                reason=f"重试 {task.retry_count} 次仍失败：{err}",
            )
        else:
            dao.release_for_retry(task.id, self.agent_id, task.version, reason=str(err))

    # ---------- 子类实现 ----------

    def process(self, task: AgentTaskModel, db) -> None:
        """执行任务；成功须自行调用 dao.write_back(...)。

        :raises TaskPermanentError: 永久错误，不重试直接 fail
        :raises Exception: 偶发错误，走重试分流
        """
        raise NotImplementedError

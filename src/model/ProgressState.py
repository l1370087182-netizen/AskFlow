"""进度状态机：前端进度面板的展示状态（与 agent_task 的生命周期状态机区分）。

两套状态各自的职责（见 service/knowledge_service.py 模块注释）：
- 生命周期状态 = agent_task.status（TaskStatus + ALLOWED_TRANSITIONS，
  存储层白名单强制），数据库是唯一真相源
- 进度状态 = Redis 进度态 kb:crawl:task:{id} 的 status 字段，允许丢失，
  只服务前端面板；读路以 DB 生命周期状态校准（view_status）

本模块是进度状态词表与「DB 状态 → 展示状态」校准规则的唯一定义处，
写入端（producer/searcher）与读路（knowledge_service/board_controller）
一律引用这里的常量，不写裸字符串。
"""
import time

from model.AgentTaskModel import TaskKind, TaskStatus


class ProgressStatus:
    """进度状态（7 态，词表见 schema/knowledge.py 的 CrawlTaskOut.status 描述）"""

    PENDING = "pending"      # 排队（含回收待重跑）
    RUNNING = "running"      # 爬取中
    SEARCHING = "searching"  # 联网检索中（web_search 任务的开局态）
    DONE = "done"            # 全部成功（或颗粒无收但属正常，如全被门禁拦截）
    PARTIAL = "partial"      # 有成有败
    FAILED = "failed"        # 失败/超时/被系统中断
    CANCELED = "canceled"    # 用户取消


# 终态：到了就不再变（view_status 只会以 DB 终态覆盖非终态）
TERMINAL = frozenset({
    ProgressStatus.DONE,
    ProgressStatus.PARTIAL,
    ProgressStatus.FAILED,
    ProgressStatus.CANCELED,
})

# 活跃：爬取/检索链路仍在进行（board_controller 判子题 waiting_crawl 用）
ACTIVE = frozenset({
    ProgressStatus.PENDING,
    ProgressStatus.RUNNING,
    ProgressStatus.SEARCHING,
})

# DB 生命周期终态 → 展示终态（view_status 校准依据；DB 是真相源）
DB_TERMINAL_MAP = {
    TaskStatus.COMPLETED: ProgressStatus.DONE,
    TaskStatus.FAILED: ProgressStatus.FAILED,
    TaskStatus.CANCELED: ProgressStatus.CANCELED,
}


def view_status(row, state: dict, heartbeat_timeout: float) -> dict:
    """用 DB 生命周期状态校准进度视图（DB 优先，防执行端崩溃/取消后视图失真）。

    ① DB canceled → canceled（合并 Redis 已有进度）；
    ② DB 终态而视图非终态 → 以 DB 覆盖（防 searcher/producer 崩溃后前端永远
       显示 running/searching）；
    ③ DB 活跃 → 状态以 DB 为准（pending 就是排队；悬挂判定只对 in_progress 做，
       pending 由 reaper 兜底，不误判失败）。

    row 是 agent_task 行；heartbeat_timeout 为前端视图的心跳超时秒数
    （引擎回收阈值见 agent_engine.reaper）。
    """
    db_status = row.status
    if db_status == TaskStatus.CANCELED:
        state["status"] = ProgressStatus.CANCELED
        if not state.get("finished_at"):
            state["finished_at"] = time.time()
        return state
    if db_status in DB_TERMINAL_MAP:
        if state["status"] not in TERMINAL:
            state["status"] = DB_TERMINAL_MAP[db_status]
            if db_status == TaskStatus.FAILED and not state.get("error"):
                state["error"] = (row.output or {}).get("error", "") or "任务执行失败"
        return state
    # DB 活跃：生命周期以 DB 为准
    if db_status == TaskStatus.PENDING:
        state["status"] = ProgressStatus.PENDING  # 排队（含回收待重跑）
    else:
        if state["status"] not in (ProgressStatus.RUNNING, ProgressStatus.SEARCHING):
            state["status"] = (
                ProgressStatus.SEARCHING
                if row.kind == TaskKind.WEB_SEARCH
                else ProgressStatus.RUNNING
            )
        if time.time() - state.get("heartbeat", 0) > heartbeat_timeout:
            state["status"] = ProgressStatus.FAILED
            state["error"] = state.get("error") or "任务超时（工作线程心跳丢失），已标记失败"
    return state

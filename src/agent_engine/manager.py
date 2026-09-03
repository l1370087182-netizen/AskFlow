"""引擎装配：幂等拉起全部 Agent + 超时回收器（main.py create_app 调用）。

启动顺序：
1. 启动自检：上次停机/崩溃遗留的执行中任务立即回收（不等 5 分钟心跳超时）
2. Producer × PRODUCER_INSTANCES（IO 等待型，实例换吞吐）
3. Reviewer × 1（质检，审核与生产分离）
4. Curator × 1（术语整理，接力懒消费）
5. Planner × 1（学习规划，用户触发低频）
6. Searcher × 1（联网搜索补爬：生成query→搜索→过滤→派生子爬取）
7. TimeoutReaper（心跳超时回收 + 重试上限，唯一集中式兜底）

注意：uvicorn --reload 会起多进程，勿在开发模式使用（重复消费）。
"""
import logging
import threading

from agent_engine.base_agent import BaseAgent
from agent_engine.reaper import TimeoutReaper
from agents.curator import CuratorAgent
from agents.planner import PlannerAgent
from agents.producer import ProducerAgent
from agents.reviewer import ReviewerAgent
from agents.searcher import SearcherAgent

logger = logging.getLogger(__name__)

# producer 并行度（设计文档 §4.3：爬取是 IO 等待型，3 实例约 ×3 吞吐；
# 不再高的理由：目标站点礼貌度 / LLM 成本 / Milvus Lite 串行入库瓶颈）
PRODUCER_INSTANCES = 3

_started = False
_start_lock = threading.Lock()
_agents: list[BaseAgent] = []
_reaper: TimeoutReaper | None = None

# Agent 类 → 中文角色名（任务板活动面板展示）
_ROLE_ZH = {
    "ProducerAgent": "爬取生产",
    "SearcherAgent": "联网检索",
    "ReviewerAgent": "知识质检",
    "CuratorAgent": "术语整理",
    "PlannerAgent": "学习规划",
}


def agent_statuses(viewer_id: int) -> list[dict]:
    """全部 agent 的状态快照（任务板活动面板数据源）。

    隐私：agent 是全局的，正在处理的任务若属于其他用户（或行已删），
    只暴露 status，不暴露 task_id/desc（防止跨用户泄漏 URL/主题）。
    """
    from DAO.agent_task_dao import AgentTaskDAO
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        dao = AgentTaskDAO(db)
        out = []
        for agent in _agents:
            act = getattr(agent, "activity", None) or {}
            item = {
                "agent_id": agent.agent_id,
                "role": _ROLE_ZH.get(agent.__class__.__name__, agent.__class__.__name__),
                "status": act.get("status", "idle"),
                "task_id": "",
                "kind": "",
                "desc": "",
                "since": act.get("since", 0),
            }
            if item["status"] == "working" and act.get("task_id"):
                row = dao.get(act["task_id"])
                if row is not None and row.user_id == viewer_id:
                    item["task_id"] = row.id
                    item["kind"] = row.kind
                    item["desc"] = act.get("desc", "")
            out.append(item)
        # reaper 不是 BaseAgent，手工拼一条
        out.append({
            "agent_id": "reaper-01",
            "role": "超时回收",
            "status": "watching",
            "task_id": "",
            "kind": "",
            "desc": "巡检心跳超时的任务并回收",
            "since": 0,
        })
        return out
    finally:
        db.close()


def start_agent_engine() -> None:
    """幂等启动整个 Agent 引擎"""
    global _started, _reaper
    with _start_lock:
        if _started:
            return
        _started = True

        TimeoutReaper.startup_sweep()

        for i in range(1, PRODUCER_INSTANCES + 1):
            agent = ProducerAgent(f"producer-{i}")
            agent.start()
            _agents.append(agent)
        for agent in (
            ReviewerAgent("reviewer-01"),
            CuratorAgent("curator-01"),
            PlannerAgent("planner-01"),
            SearcherAgent("searcher-01"),
        ):
            agent.start()
            _agents.append(agent)

        _reaper = TimeoutReaper()
        _reaper.start()

        logger.info(
            "[agent-engine] 已启动：producer×%s + reviewer + curator + planner + searcher + reaper",
            PRODUCER_INSTANCES,
        )

"""引擎装配：幂等拉起全部 Agent + 超时回收器（main.py create_app 调用）。

启动顺序：
1. 启动自检：上次停机/崩溃遗留的执行中任务立即回收（不等 5 分钟心跳超时）
2. Producer × PRODUCER_INSTANCES（IO 等待型，实例换吞吐）
3. Reviewer × 1（质检，审核与生产分离）
4. Curator × 1（术语整理，接力懒消费）
5. TimeoutReaper（心跳超时回收 + 重试上限，唯一集中式兜底）

注意：uvicorn --reload 会起多进程，勿在开发模式使用（重复消费）。
"""
import logging
import threading

from agent_engine.base_agent import BaseAgent
from agent_engine.reaper import TimeoutReaper
from agents.curator import CuratorAgent
from agents.producer import ProducerAgent
from agents.reviewer import ReviewerAgent

logger = logging.getLogger(__name__)

# producer 并行度（设计文档 §4.3：爬取是 IO 等待型，3 实例约 ×3 吞吐；
# 不再高的理由：目标站点礼貌度 / LLM 成本 / Milvus Lite 串行入库瓶颈）
PRODUCER_INSTANCES = 3

_started = False
_start_lock = threading.Lock()
_agents: list[BaseAgent] = []
_reaper: TimeoutReaper | None = None


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
        for agent in (ReviewerAgent("reviewer-01"), CuratorAgent("curator-01")):
            agent.start()
            _agents.append(agent)

        _reaper = TimeoutReaper()
        _reaper.start()

        logger.info(
            "[agent-engine] 已启动：producer×%s + reviewer + curator + reaper",
            PRODUCER_INSTANCES,
        )

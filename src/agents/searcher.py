"""Searcher Agent：联网搜索补爬（生成 query → 博查搜索 → 过滤 → 派生子爬取）。

任务输入（payload）：{topic, source}
职责边界（与 producer 同构）：
- 生命周期（认领/重试/失败）由任务引擎管，本类只写结果
- 进度态复用 kb:crawl:task:{id}（status=searching + phase 阶段），
  前端爬取面板同一端点轮询即可见
- 心跳兼作取消探针：每阶段间调 dao.heartbeat，False = 被取消/回收 → 即停
- 先 write_back 后建子任务：写回成功才派生 CRAWL 子任务（取消/回收抢先时
  不产生孤儿爬取任务）；子任务关联走 parent_id 边，读路按 find_child 反查
"""
import logging
import time

import redis

from DAO.agent_task_dao import AgentTaskDAO
from agent_engine.base_agent import BaseAgent, TaskPermanentError
from generation.llm import build_llm_for_user
from model.AgentTaskModel import TaskKind, TaskStatus
from search.web_search import WebSearchError, filter_candidates, generate_queries, search_web
from service.knowledge_service import (
    _redis,
    _save_task,
    submit_crawl,
    CrawlSubmitError,
)

logger = logging.getLogger(__name__)


def _try_save(r, state: dict) -> None:
    """进度态写入尽力而为：Redis 只服务前端面板，丢了不影响任务执行"""
    try:
        _save_task(r, state)
    except redis.RedisError as e:
        logger.warning("[searcher] 进度态写入失败（任务照常执行）：%s", e)


class SearcherAgent(BaseAgent):
    """联网检索：选页的智能部分在这，爬取交给 producer 的子任务"""

    KINDS = [TaskKind.WEB_SEARCH]

    def process(self, task, db) -> None:
        payload = task.payload or {}
        topic = str(payload.get("topic", "")).strip()
        if not topic:
            raise TaskPermanentError("缺少检索主题")
        dao = AgentTaskDAO(db)
        r = _redis()

        # 1) 生成检索词与过滤候选都要用用户自己的模型（与爬取同口径）
        llm = build_llm_for_user(db, task.user_id)
        if llm is None:
            self._save_state(r, task, {
                "status": "failed",
                "error": "未配置个人大模型，请先到「对话学习」页 ⚙️ 配置模型",
            })
            raise TaskPermanentError("未配置个人大模型")

        # 2) 进度态开局：searching（前端不定长动画条）
        state = self._save_state(r, task, {"status": "searching", "phase": "生成检索词"})
        self._note(f"生成检索词：{topic[:30]}")

        # 3) 搜索 + 选页（每阶段间心跳：防 reaper 误回收 + 取消探针）
        try:
            if not self._check_alive(dao, r, task, state):
                return
            queries = generate_queries(llm, topic)
            state["phase"] = "搜索候选网页"
            self._note(f"搜索候选网页：{topic[:30]}")
            _try_save(r, state)

            candidates: list[dict] = []
            seen: set[str] = set()
            for q in queries:
                if not self._check_alive(dao, r, task, state):
                    return
                try:
                    from core.config import settings
                    hits = search_web(q, count=settings.SEARCH_MAX_RESULTS)
                except WebSearchError as e:
                    logger.warning("[searcher] 检索词「%s」搜索失败：%s", q, e)
                    continue
                for h in hits:
                    if h["url"] not in seen:
                        seen.add(h["url"])
                        candidates.append(h)

            if not self._check_alive(dao, r, task, state):
                return
            state["phase"] = "筛选有价值网页"
            self._note(f"筛选候选网页：{len(candidates)} 条")
            _try_save(r, state)
            selected = filter_candidates(llm, db, task.user_id, topic, candidates)
        except Exception as e:  # noqa: BLE001 —— 意外异常：进度态置失败后走引擎重试
            state["status"] = "failed"
            state["error"] = f"检索执行异常：{e}"
            state["finished_at"] = time.time()
            _try_save(r, state)
            raise

        if not self._check_alive(dao, r, task, state):
            return

        # 4) 无有价值结果：也是合法终态（模型判定都不值得爬）
        if not selected:
            dao.write_back(
                task.id, self.agent_id, task.version,
                status=TaskStatus.COMPLETED,
                output={"topic": topic, "queries": queries, "selected": []},
                log_action="complete",
                log_desc=f"联网检索完成：{len(candidates)} 个候选均不值得爬取",
            )
            state["status"] = "done"
            state["phase"] = ""
            state["error"] = f"搜索到 {len(candidates)} 个候选，筛选后无值得入库的页面"
            state["finished_at"] = time.time()
            _try_save(r, state)
            logger.info("[searcher:%s] 任务 %s 完成：无值得爬取的候选", self.agent_id, task.id)
            return

        # 5) 先写回再建子任务：写回被取消/回收抢先（None）就不派生孤儿爬取任务
        wrote = dao.write_back(
            task.id, self.agent_id, task.version,
            status=TaskStatus.COMPLETED,
            output={"topic": topic, "queries": queries, "selected": selected},
            log_action="complete",
            log_desc=f"联网检索完成：选中 {len(selected)} 个页面，转入爬取",
        )
        if wrote is None:
            logger.info("[searcher:%s] 任务 %s 写回冲突（已取消/回收），不派生爬取",
                        self.agent_id, task.id)
            return

        # 6) 派生子爬取任务（parent_id 关联；子任务由 producer 并行消费）
        child_id = ""
        try:
            child_id = submit_crawl(
                db,
                uid=task.user_id,
                urls=[s["url"] for s in selected],
                category="general",
                max_pages=len(selected),
                topic=topic,
                parent_id=task.id,
            )
        except CrawlSubmitError as e:
            # 活跃爬取达上限等：检索结果丢弃，子题照常编材料（补爬是增强不是依赖）
            logger.warning("[searcher:%s] 子爬取未提交（%s），检索结果放弃", self.agent_id, e)
        state["status"] = "done"
        state["phase"] = ""
        state["child_task_id"] = child_id
        state["finished_at"] = time.time()
        _try_save(r, state)
        logger.info(
            "[searcher:%s] 任务 %s 完成：选中 %s 页 → 子爬取 %s",
            self.agent_id, task.id, len(selected), child_id or "-",
        )

    def _check_alive(self, dao, r, task, state: dict) -> bool:
        """心跳探针：False = 已被取消/回收 → 写终态进度后返回（调用方直接 return）"""
        state["heartbeat"] = time.time()
        if dao.heartbeat(task.id, self.agent_id):
            _try_save(r, state)
            return True
        row = dao.get(task.id)
        if row is not None and row.status == TaskStatus.CANCELED:
            state["status"] = "canceled"
        else:
            state["status"] = "failed"
            state["error"] = "任务被系统中断（超时回收或状态变更），本次执行终止"
        state["finished_at"] = time.time()
        _try_save(r, state)
        logger.info("[searcher:%s] 任务 %s 提前终止：%s", self.agent_id, task.id, state["status"])
        return False

    @staticmethod
    def _save_state(r, task, updates: dict) -> dict:
        """进度态覆写（复用爬取面板的数据形状，前端同一端点轮询）"""
        payload = task.payload or {}
        state = {
            "task_id": task.id,
            "uid": task.user_id,
            "url": "",
            "category": "general",
            "max_pages": 0,
            "status": "searching",
            "topic": payload.get("topic", ""),
            "phase": "",
            "done_pages": 0,
            "failed_pages": 0,
            "skipped_pages": 0,
            "current_url": "",
            "pages": [],
            "error": "",
            "heartbeat": time.time(),
            "created_at": task.created_at.timestamp() if task.created_at else time.time(),
            "finished_at": 0.0,
            "child_task_id": "",
        }
        state.update(updates)
        _try_save(r, state)
        return state

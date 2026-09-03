"""Producer Agent：整站浅爬任务（爬取 → AI 清洗 → upsert → 即时向量化）。

任务输入（payload）：{url, category, max_pages}
职责边界（设计文档 §4.2）：
- 生命周期（认领/重试/失败）由任务引擎管理，本类只写结果
- 逐页进度态写 Redis（kb:crawl:task:{id}，前端进度面板实时数据，可丢失）
- 向量化用进程级锁串行（Milvus Lite 单文件，线程并发无官方保证）
"""
import logging
import threading
import time

import redis

from DAO.agent_task_dao import AgentTaskDAO
from agent_engine.base_agent import BaseAgent, TaskPermanentError
from generation.llm import build_llm_for_user
from model.AgentTaskModel import TaskKind, TaskStatus
from model.KnowledgeModel import KnowledgeModel
from DAO.knowledge_dao import KnowledgeDAO
from milvus.ingestion.pipeline import IngestionPipeline
from service.knowledge_service import (
    _redis,
    _save_task,
    clean_page_content,
)
from spider.shallow_crawler import ShallowCrawler
from spider.tech_spider import TechSpider

logger = logging.getLogger(__name__)

# Milvus Lite 单文件单例，并行任务的向量化必须串行写
_ingest_lock = threading.Lock()


def _try_save(r, state: dict) -> None:
    """进度态写入尽力而为：Redis 只服务前端面板，丢了不影响任务执行"""
    try:
        _save_task(r, state)
    except redis.RedisError as e:
        logger.warning("[producer] 进度态写入失败（任务照常执行）：%s", e)


class ProducerAgent(BaseAgent):
    """爬取生产者：唯一强制多实例的角色（IO 等待型，实例换吞吐）"""

    KINDS = [TaskKind.CRAWL]

    def process(self, task, db) -> None:
        payload = task.payload or {}
        url = payload.get("url", "")
        urls = payload.get("urls") or []      # 联网补爬：显式页面列表，逐页直抓不扩链
        category = payload.get("category", "general")
        try:
            max_pages = int(payload.get("max_pages", 10))
        except (TypeError, ValueError):
            max_pages = 10
        dao = AgentTaskDAO(db)
        r = _redis()

        # 1) 清洗必须用用户自己的模型（提交时查过一次，执行前再查防中途清空）
        llm = build_llm_for_user(db, task.user_id)
        if llm is None:
            self._save_state(r, task, {
                "status": "failed",
                "error": "未配置个人大模型，请先到「对话学习」页 ⚙️ 配置模型后再爬取",
            })
            raise TaskPermanentError("未配置个人大模型")

        # 2) 进度态开局：重试续跑也重置（重新从种子页爬，旧页结果不沿用）
        state = self._save_state(r, task, {"status": "running", "pages": []})

        # 3) 逐页：抓取 → 校验 → AI 清洗 → upsert → 即时向量化
        crawler = ShallowCrawler(max_pages=max_pages)
        page_source = crawler.iter_urls(urls) if urls else crawler.iter_pages(url)
        try:
            for page in page_source:
                state["current_url"] = page["url"]
                state["heartbeat"] = time.time()
                _try_save(r, state)
                # 取消检查点 1（页头）：心跳命中 0 行 = 被取消/回收，立即终止。
                # 取消不回滚已入库数据：此前 upsert 的页面保留在知识库。
                if not dao.heartbeat(task.id, self.agent_id):
                    self._abort(dao, r, task, state)
                    return

                if not page["ok"]:
                    # 抓取失败：记入失败页，逐页降级继续
                    state["failed_pages"] += 1
                    state["pages"].append({
                        "url": page["url"], "ok": False, "cleaned": False,
                        "knowledge_id": None, "error": page.get("error", "抓取失败"),
                    })
                    _try_save(r, state)
                    continue

                if not TechSpider.is_valid_article(page):
                    # 正文太短（导航页/空页）：无知识价值，跳过不入库
                    state["skipped_pages"] += 1
                    _try_save(r, state)
                    continue

                # 取消检查点 2（AI 清洗前）：清洗是单页最耗时步骤（秒级~1 分钟），
                # 多查一次让取消尽快生效
                if not dao.heartbeat(task.id, self.agent_id):
                    self._abort(dao, r, task, state)
                    return

                # AI 清洗（失败自动回退原文）→ upsert → 即时向量化
                title = page.get("title") or page["url"]
                cleaned_text, cleaned = clean_page_content(llm, title, page["content"])
                row = KnowledgeDAO(db).upsert(
                    title=title,
                    content=cleaned_text,
                    source_url=page["url"],
                    category=category,
                    source_type="personal",
                    user_id=task.user_id,
                )
                knowledge_id = row.id if row else None
                if row is not None and row.status == KnowledgeModel.STATUS_PENDING:
                    try:
                        with _ingest_lock:
                            IngestionPipeline(db).ingest_row(row)
                    except Exception:  # noqa: BLE001 —— 单页向量化失败不中断任务
                        logger.exception("[producer] 页面 %s 向量化失败", page["url"])

                state["done_pages"] += 1
                state["pages"].append({
                    "url": page["url"], "ok": True, "cleaned": cleaned,
                    "knowledge_id": knowledge_id, "error": "",
                })
                _try_save(r, state)
        except Exception as e:  # noqa: BLE001 —— 意外异常：进度态置失败后抛出走引擎重试
            state["status"] = "failed"
            state["error"] = f"任务执行异常：{e}"
            _try_save(r, state)
            raise

        # 4) 终态判定：全成=done；有成有败=partial；颗粒无收=failed
        if state["done_pages"] > 0 and state["failed_pages"] == 0:
            state["status"] = "done"
        elif state["done_pages"] > 0:
            state["status"] = "partial"
        else:
            state["status"] = "failed"
            first_err = next((p["error"] for p in state["pages"] if p.get("error")), "")
            state["error"] = first_err or "未能爬到任何有效页面"
        state["finished_at"] = time.time()
        _try_save(r, state)

        # 5) 写回任务引擎（幂等 CAS；被取消/回收过的任务此处自动放弃）
        output = {
            "done_pages": state["done_pages"],
            "failed_pages": state["failed_pages"],
            "skipped_pages": state["skipped_pages"],
        }
        summary = (
            f"成功 {state['done_pages']} / 失败 {state['failed_pages']}"
            f" / 跳过 {state['skipped_pages']}"
        )
        if state["status"] == "failed":
            wrote = dao.write_back(
                task.id, self.agent_id, task.version,
                status=TaskStatus.FAILED,
                output={**output, "error": state.get("error", "")},
                log_action="fail", log_desc=summary,
            )
        else:
            wrote = dao.write_back(
                task.id, self.agent_id, task.version,
                status=TaskStatus.COMPLETED,
                output=output,
                log_action="complete", log_desc=summary,
            )
        if wrote is None:
            # 取消/回收抢先：结果丢弃，也绝不派生质检子任务（防孤儿任务）
            logger.info(
                "[producer:%s] 任务 %s 写回冲突（已取消或被回收），丢弃结果",
                self.agent_id, task.id,
            )
            return
        # 接力：写回成功且有入库条目 → 发质检子任务（审核与生产分离）
        if state["status"] != "failed":
            knowledge_ids = [
                p["knowledge_id"] for p in state["pages"]
                if p.get("ok") and p.get("knowledge_id")
            ]
            if knowledge_ids:
                dao.create(
                    kind=TaskKind.QUALITY_REVIEW,
                    user_id=task.user_id,
                    payload={"knowledge_ids": knowledge_ids, "crawl_task_id": task.id},
                    parent_id=task.id,
                    trace_id=task.trace_id,
                    agent=self.agent_id,
                )
        logger.info("[producer:%s] 任务 %s 结束：%s（%s）",
                    self.agent_id, task.id, state["status"], summary)

    def _abort(self, dao, r, task, state: dict) -> None:
        """心跳探针命中 0 行 → 回读真实状态定性后终止（不 write_back、不建子任务）。

        三种中断：用户取消 → 进度态置 canceled；reaper 回收/判败 → 置中断说明
        （回收的任务会被重新认领重跑，届时进度态重置）。
        """
        row = dao.get(task.id)
        if row is not None and row.status == TaskStatus.CANCELED:
            state["status"] = "canceled"
            state["error"] = ""
        else:
            state["status"] = "failed"
            state["error"] = "任务被系统中断（超时回收或状态变更），本次执行终止"
        state["finished_at"] = time.time()
        _try_save(r, state)
        logger.info(
            "[producer:%s] 任务 %s 提前终止：%s（已入库页面保留）",
            self.agent_id, task.id, state["status"],
        )

    @staticmethod
    def _save_state(r, task, updates: dict) -> dict:
        """进度态覆写：任务 id 与入参沿用 agent_task，字段缺省补齐"""
        payload = task.payload or {}
        state = {
            "task_id": task.id,
            "uid": task.user_id,
            "url": payload.get("url", "") or (payload.get("urls") or [""])[0],
            "category": payload.get("category", "general"),
            "max_pages": int(payload.get("max_pages", 10)),
            "status": "pending",
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

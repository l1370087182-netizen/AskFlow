"""
  scheduler.py —— 调度器（任务分发、队列管理）

  职责：
      1. 从 sites.py 读入口任务，灌入 Redis 队列（同时把入口 URL 写入去重集合）
      2. 启动多个 Worker 并发消费
      3. 结束时打印统计
"""
import time

from spider.sites import all_start_tasks
from spider.task_queue import TaskQueue
from spider.deduplicator import URLDeduplicator
from spider.worker import run_workers


class SpiderScheduler:
    """爬虫调度器"""

    def __init__(self):
        self.queue = TaskQueue()
        self.dedup = URLDeduplicator()

    def seed(self, reset: bool = False) -> int:
        """
        把 sites.py 的入口任务灌入队列。
        :param reset: True=全量模式，先清空队列和已访问集合再灌入；
                      False=增量模式，已访问过的入口 URL 不再重复入队（可幂等重跑）
        :return: 本次新入队的任务数
        """
        if reset:
            self.queue.clear()
            self.dedup.clear()

        tasks = all_start_tasks()

        # 入口 URL 和普通扩链一样走 dedup.add：
        # - 全量模式刚清空过集合，所有入口都会入队
        # - 增量模式下，已访问过的入口返回 False，直接跳过
        fresh = [t for t in tasks if self.dedup.add(t["url"])]

        if fresh:
            self.queue.push_many(fresh)
        return len(fresh)

    def run(self, n_workers: int = 2, reset: bool = False) -> None:
        """
        一键启动：检查 Redis -> 灌种子 -> 启动 workers -> 打印统计
        :param n_workers: 并发 worker 数
        :param reset: 是否全量重爬
        """
        # 提前检查，避免跑了一半才报 Redis 连不上
        if not self.queue.ping():
            raise RuntimeError("Redis 不可用，请先启动 Redis")

        seeded = self.seed(reset=reset)
        mode = "全量" if reset else "增量"
        print(f"[scheduler] {mode}模式：新入队 {seeded} 个入口任务，当前队列长度 {self.queue.size()}")

        start = time.time()
        run_workers(n_workers)
        elapsed = time.time() - start

        stats = self.status()
        print(
            f"[scheduler] 爬取结束，耗时 {elapsed:.1f}s | "
            f"已访问 URL {stats['visited']} 个 | 队列剩余 {stats['queue']} 个"
        )

    def status(self) -> dict:
        """
        队列统计，后续 /api/spider/status 接口直接复用
        :return: {"queue": 队列剩余任务数, "visited": 已访问URL数}
        """
        return {
            "queue": self.queue.size(),
            "visited": self.dedup.size(),
        }

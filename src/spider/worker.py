"""
  worker.py —— 爬虫 Worker（并发爬取）

  职责：循环从 Redis 队列取任务 → 抓取解析 → upsert 入库 → 新链接回灌队列。
  多个 Worker 线程并发消费同一个队列，互不冲突（队列 blpop 和去重 sadd 都是原子的）。
"""
import threading
import traceback

from database.session import SessionLocal
from DAO.knowledge_dao import KnowledgeDAO
from spider.task_queue import TaskQueue
from spider.deduplicator import URLDeduplicator
from spider.tech_spider import TechSpider

class SpiderWorker:
    """爬虫 Worker"""

    def __init__(self, worker_id: int = 0):
        self.worker_id = worker_id
        self.queue = TaskQueue()
        self.dedup = URLDeduplicator()
        self.spider = TechSpider()
        self._running = False

    def process_task(self, task: dict) -> None:
        """处理一个任务：抓取->入库->扩展链路入队"""
        # 1. 抓取+解析
        article, new_links = self.spider.crawl(task)

        # 2. 有价值的正文才入库：每个线程用独立Session，保证线程安全
        if TechSpider.is_valid_article(article):
            db = SessionLocal()
            try:
                dao = KnowledgeDAO(db)
                row = dao.upsert(
                    title=article["title"],
                    content=article["content"],
                    source_url=article["source_url"],
                    category=article["category"],
                    source_type="spider"
                )
                print(f"[worker-{self.worker_id}] 入库: {article['title'][:30]}")
            finally:
                db.close()

        else:
            print(f"[worker-{self.worker_id}] 无价值正文，跳过入库: {task['url']}")

        # 3. 新链接先去重（sadd 返回 True 说明首次发现），再批量回灌队列
        fresh = [link for link in new_links if self.dedup.add(link)]
        if fresh:
            tasks = [
                {
                    "url": link,
                    "category": task.get("category", "general"),
                    "site": task.get("site", "unknown"),
                    "allowed_prefix": task.get("allowed_prefix", ""),
                }
                for link in fresh
            ]
            self.queue.push_many(tasks)
            print(f"[worker-{self.worker_id}] 扩展 {len(fresh)} 个新链接入队")
    def run(self, idle_timeout: int = 5, max_idle_rounds: int = 6) -> None:
        """
        Worker 主循环
        :param idle_timeout: 每次阻塞出队的等待秒数
        :param max_idle_rounds: 连续多少轮取不到任务就自动退出（爬虫自然结束）
        """
        self._running = True
        idle_rounds = 0
        print(f"[worker-{self.worker_id}] 启动")

        while self._running:
            task = self.queue.pop(timeout=idle_timeout)
            if task is None:
                idle_rounds += 1
                if idle_rounds >= max_idle_rounds:
                    print(f"[worker-{self.worker_id}] 队列已空，退出")
                    break
                continue

            idle_rounds = 0
            try:
                self.process_task(task)
            except Exception as e:
                # 单个任务失败不影响整体，记录后继续
                print(f"[worker-{self.worker_id}] 任务失败 {task.get('url')}: {e}")
                traceback.print_exc()

        self._running = False

    def stop(self) -> None:
        """通知 Worker 停止"""
        self._running = False

def run_workers(n: int = 2) -> None:
    """启动 n 个 Worker 线程，阻塞直到全部结束"""
    workers = [SpiderWorker(i) for i in range(n)]
    threads = [
        threading.Thread(target=w.run, name=f"worker-{i}")
        for i, w in enumerate(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
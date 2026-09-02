"""
任务队列：基于 Redis List 的分布式队列。
只负责 push / pop / 长度，不去重（去重交给 deduplicator）。
"""

import redis
from typing import Any
import json

from util.redis_util import make_redis

class TaskQueue:
    """爬虫任务队列"""

    def __init__(self, queue_key: str="crawl:queue") -> None:
        self.queue_key = queue_key
        # 统一工厂：短超时+失败即抛（blpop 的等待由 worker 外层循环负责）
        self.r = make_redis()

    def ping(self) ->bool:
        """检查Redis连接"""
        return bool(self.r.ping())

    def push(self, task: dict[str, Any]) -> None:
        """添加任务到队列(右侧进入)"""
        self.r.rpush(self.queue_key, json.dumps(task, ensure_ascii=False))
    
    def push_many(self, tasks: list[dict[str, Any]]) -> int:
        """批量添加任务到队列(右侧进入)"""
        if not tasks:
            return 0
        pipe = self.r.pipeline()
        for task in tasks:
            pipe.rpush(self.queue_key, json.dumps(task, ensure_ascii=False))
        pipe.execute()
        return len(tasks)

    def pop(self, timeout: int=5) -> dict[str, Any] | None:
        """
        阻塞出队（左侧出，多 worker 安全）。
        timeout 秒内无任务返回 None。
        """
        try:
            item = self.r.blpop(self.queue_key, timeout=timeout)
        except redis.exceptions.TimeoutError:
            # redis-py 已知竞态：socket 超时偶尔先于 BLPOP 的 nil 响应到达。
            # 队列契约是「没任务返回 None」，不能因此杀死 worker 线程
            return None
        if item is None:
            return None
        _, raw = item
        return json.loads(raw)

    def size(self) -> int:
        """获取队列长度"""
        return self.r.llen(self.queue_key)

    def clear(self) -> None:
        """清空队列"""
        self.r.delete(self.queue_key)


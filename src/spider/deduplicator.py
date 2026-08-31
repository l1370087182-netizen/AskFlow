import redis
from core.config import settings



class URLDeduplicator:
    """URL去重器"""

    def __init__(self, key: str="crawl:visited") -> None:
        self.key = key
        self.r = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=True,
        )

    def seen(self, url: str) -> bool:
        """检查URL是否已访问过"""
        return bool(self.r.sismember(self.key, url))

    def add(self, url: str) -> bool:
        """
        标记URL为已访问
        返回True: 第一次加入，False: 早已存在
        """
        return self.r.sadd(self.key, url) == 1

    def add_many(self, urls: list[str]) -> int:
        """
        批量加入URL集合
        返回新加入的数量
        """
        if not urls:
            return 0
        pipe = self.r.pipeline()
        for u in urls:
            pipe.sadd(self.key, u)
        results = pipe.execute()
        return sum(1 for x in results if x == 1)

    def size(self) -> int:
        """返回集合中URL的数量"""
        return int(self.r.scard(self.key))

    def clear(self) -> None:
        """清空（调试用）"""
        self.r.delete(self.key)




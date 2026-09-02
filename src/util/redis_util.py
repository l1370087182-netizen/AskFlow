"""统一 Redis 客户端工厂：短超时 + 失败即抛（不做连接重试）。

本项目所有 Redis 用途（缓存/进度态/锁/队列）调用方都有明确降级路径，
而 redis-py 默认 3 次连接重试 + 指数退避会在 Redis 不可达（尤其端口
黑洞态）时把单次调用放大到 25s+——task_queue 早年已踩过此坑单独关过
重试，现收编为全局统一策略：

    socket_connect_timeout=2  建连最多等 2 秒
    socket_timeout=5          命令读写最多等 5 秒
    retry=0 次                首次失败即抛，降级交给调用方
"""
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from core.config import settings

_FAIL_FAST = Retry(NoBackoff(), 0)


def make_redis() -> redis.Redis:
    """项目统一的 Redis 客户端（失败即抛，调用方自行降级）"""
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        retry=_FAIL_FAST,
    )

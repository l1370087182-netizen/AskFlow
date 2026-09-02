"""认领锁：两层锁认领的第一层（Redis SETNX + Lua 比对释放）。

锁只负责减少多 Agent 同时抢同一任务的无效竞争；
真正的正确性由第二层（AgentTaskDAO 的 CAS）保证——
所以 Redis 不可用时选择「放行」（降级为纯 CAS 竞争），绝不阻塞任务执行。
"""
import logging

import redis

from util.redis_util import make_redis

logger = logging.getLogger(__name__)

# Lua：只有 value 是自己时才允许删（禁止裸 delete 误删他人的锁）
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def _redis() -> redis.Redis:
    """Redis 客户端（统一工厂：短超时+失败即抛，见 util/redis_util）"""
    return make_redis()


class ClaimLock:
    """任务认领锁：TTL 内互斥，过期自动释放（防进程死亡死锁）"""

    KEY = "agent:lock:{task_id}"

    def __init__(self, ttl: int = 600):
        self.ttl = ttl

    def acquire(self, task_id: str, owner: str) -> bool:
        """抢锁；Redis 故障时放行（CAS 兜底正确性）"""
        try:
            return bool(
                _redis().set(self.KEY.format(task_id=task_id), owner, nx=True, ex=self.ttl)
            )
        except redis.RedisError as e:
            logger.warning("[agent-lock] Redis 不可用，降级为无锁竞争：%s", e)
            return True

    def release(self, task_id: str, owner: str) -> None:
        """Lua 比对 value 后释放；失败不抛（锁有 TTL 兜底过期）"""
        try:
            _redis().eval(_RELEASE_LUA, 1, self.KEY.format(task_id=task_id), owner)
        except redis.RedisError as e:
            logger.warning("[agent-lock] 释放锁失败（TTL 兜底）：%s", e)

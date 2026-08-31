import random
import httpx
import time



USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/118.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
        "Gecko/20100101 Firefox/121.0"
    ),
]

class SpiderMiddleware:
    """请求中间件"""
    def __init__(
        self,
        max_retries: int=3,
        backoff: float=1.0,
        proxy: str | None=None,
    ) -> None:
        self.max_retries = max_retries
        self.backoff = backoff
        self.proxy = proxy

    def random_headers(self) -> dict[str, str]:
        """每次请求时随机选择一个User-Agent"""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def get(
        self,
        url: str,
        timeout: float=20.0
    ) -> httpx.Response:
        """
        带 UA 轮换 + 重试的 GET。
        全部失败则抛出最后一次异常。
        """
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(
                    headers=self.random_headers(),
                    timeout=timeout,
                    follow_redirects=True,
                    proxy=self.proxy,
                ) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    return resp
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < self.max_retries:
                    # 简单退避：1s, 2s, 3s...
                    time.sleep(self.backoff * attempt)
        assert last_exc is not None
        raise last_exc
        





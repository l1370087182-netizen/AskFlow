"""浅层爬虫：目标页 + 同域链接的 BFS 抓取（只抓不存）。

个人知识库「爬取整站」用：用户提交种子 URL，系统抓取目标页与同域链接，
默认 ≤10 页（提交侧钳制 1..20）。逐页独立产出结果，入库/清洗由调用方决定，
任何单页失败不影响其余页面。
"""
from __future__ import annotations

import logging
from collections import deque
from urllib.parse import urlparse

from spider.spider_util import crawl_one

logger = logging.getLogger(__name__)

# 二进制/非页面后缀：没有知识价值也解析不了，扩链时直接剔除
BINARY_SUFFIXES = {
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".msi", ".apk", ".dmg", ".iso",
    ".whl", ".egg", ".bin", ".dat", ".css", ".js",
}


class ShallowCrawler:
    """BFS 浅层爬虫：只抓不存，逐页 yield 结果"""

    def __init__(self, max_pages: int = 10, delay: float = 1.0):
        self.max_pages = max_pages
        self.delay = delay

    def iter_pages(self, seed_url: str):
        """BFS 逐页产出：{url, ok, title?, content?, links?, error?}

        - 复用 crawl_one（middleware 含 3 次重试 + 退避 + UA 轮换）
        - 扩链过滤：同 netloc + http(s) + 剔除二进制后缀 + 未见过 + 未达 max_pages
        - fragment 归一化：#xxx 变体与本体视为同一页（parse_article 已剥过一次，
          这里再防御一遍）
        - 种子页抓取失败 → 产出单条 ok=False 后终止
        """
        seed_url = urlparse(seed_url)._replace(fragment="").geturl()
        seed_netloc = urlparse(seed_url).netloc
        seen: set[str] = {seed_url}
        queue: deque[str] = deque([seed_url])
        visited = 0

        while queue and visited < self.max_pages:
            url = queue.popleft()
            visited += 1

            try:
                data = crawl_one(url, self.delay)
            except Exception as e:  # noqa: BLE001 —— 单页失败不抛给调用方
                logger.warning("[shallow-crawl] 抓取失败 %s：%s", url, e)
                yield {"url": url, "ok": False, "error": f"抓取失败：{e}"}
                if url == seed_url:
                    return  # 种子页失败 → 整个任务终止
                continue

            yield {
                "url": url,
                "ok": True,
                "title": data.get("title", ""),
                "content": data.get("content", ""),
                "links": data.get("links", []),
            }

            # 已达上限不再扩链（已抓的页照常产出）
            if visited >= self.max_pages:
                continue

            for link in data.get("links", []):
                parsed = urlparse(link)
                if parsed.scheme not in ("http", "https"):
                    continue
                if parsed.netloc != seed_netloc:
                    continue
                path = parsed.path.lower()
                if any(path.endswith(sfx) for sfx in BINARY_SUFFIXES):
                    continue
                # fragment 归一化后再判重
                link = parsed._replace(fragment="").geturl()
                if link in seen or len(seen) >= self.max_pages:
                    continue
                seen.add(link)
                queue.append(link)

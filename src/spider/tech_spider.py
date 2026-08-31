"""
  tech_spider.py —— 技术知识爬虫（具体爬取逻辑）

  职责：拿到一个任务 {url, category, site, allowed_prefix}，
  完成「抓取 → 解析 → 过滤站内链接」，返回文章数据和新链接。
"""
from spider.spider_util import crawl_one

class TechSpider:
    """技术知识爬虫"""

    def __init__(self, delay: float=1.0) -> None:
        self.delay = delay
    
    def crawl(self, task: dict) -> tuple[dict, list[str]]:
        """
          执行一个爬取任务
          :param task: {"url":..., "category":..., "site":..., "allowed_prefix":...}
          :return: (文章数据, 过滤后的新链接列表)
        """
        url = task["url"]
        allowed_prefix = task.get("allowed_prefix", "")

        # 1. 抓取 + 解析（复用已有的 crawl_one）
        data = crawl_one(url, self.delay)

        # 2. 扩展链接只保留allowed_prefix内的，防止爬出站外
        new_links = [
            link for link in data.get("links", [])
            if not allowed_prefix or link.startswith(allowed_prefix)
        ]

        article = {
            "title": data["title"],
            "content": data["content"],
            "source_url": url,
            "category": task.get("category", "general"),
        }
        return article, new_links

    @staticmethod
    def is_valid_article(article: dict, min_len: int = 200) -> bool:
        """
          判断文章是否有效，正文太短的页面（导航页/空页）没有知识价值，不入库
        """
        return len(article["content"]) >= min_len



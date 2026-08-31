"""
爬虫站点配置。
分布式任务从这里读取入口URL，再扩链抓取
"""

from dataclasses import dataclass

@dataclass(frozen=True)
class CrawlSite:
    name: str   # 站点名称
    category: str   # 写入knowledge.category
    start_urls: tuple[str, ...] # 入口页，元组，元素类型string，...长度不限
    allowed_prefix: str # 允许的URL前缀

# 目前先开2个站点
CRAWL_SITES: tuple[CrawlSite, ...] = (
    CrawlSite(
        name="fastapi",
        category="fastapi",
        start_urls=("https://fastapi.tiangolo.com/zh/",),
        allowed_prefix="https://fastapi.tiangolo.com/zh/",
    ),
    CrawlSite(
        name="python-tutorial",
        category="python",
        start_urls=("https://docs.python.org/zh-cn/3/tutorial/",),
        allowed_prefix="https://docs.python.org/zh-cn/3/tutorial/",
    ),
)

def get_site(name: str) -> CrawlSite | None:
    """根据站点名称获取站点配置"""
    for site in CRAWL_SITES:
        if site.name == name:
            return site
    return None

def all_start_tasks() -> list[dict]:
    """
    生成初始任务列表，供 task_manager 入队。
    每项: {url, category, site}
    """
    tasks = []
    for site in CRAWL_SITES:
        for url in site.start_urls:
            tasks.append(
                {
                    "url": url,
                    "category": site.category,
                    "site": site.name,
                    "allowed_prefix": site.allowed_prefix,
                }
            )
    return tasks






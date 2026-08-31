"""爬虫底层工具：发HTTP请求、解析标题和正文"""
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
import time
from spider.middleware import SpiderMiddleware

DEFAULT_DELAY= 1.0

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
}

_middleware = SpiderMiddleware(max_retries=3, backoff=1.0)

def fetch_html(url: str, timeout: float = 20.0) -> str:
    """发送HTTP请求，获取HTML内容"""
    response = _middleware.get(url, timeout=timeout)
    return response.text

def parse_article(html: str, url: str) -> dict:
    """解析HTML内容，获取标题和正文"""

    soup = BeautifulSoup(html, "lxml")

    # 去除标题锚点
    for a in soup.select("a.headerlink, a.md-clipboard, a[aria-hidden='true']"):
        a.decompose()

    # 获取标题
    title = ""
    if soup.title and soup.title.get_text():
        title = soup.title.get_text().strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)

    # 获取正文
    main = (
        soup.find("article")
        or soup.find("div", class_="md-content")
        or soup.body
    )
    # 去掉脚本、样式
    if main:
        for tag in main.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        content = main.get_text("\n", strip=True)
    else:
        content = soup.get_text("\n", strip=True)

    # ----- 同站链接（给分布式队列扩爬用）-----
    base_host = urlparse(url).netloc
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc != base_host:
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean not in links:
            links.append(clean)

    return {
        "title": title or url,
        "content": content,
        "links": links,
    }

def crawl_one(url: str, delay: float = DEFAULT_DELAY) -> dict:
    """抓取并解析单个URL"""
    if delay > 0:
        time.sleep(delay)
    html = fetch_html(url)
    data = parse_article(html, url)
    data["source_url"] = url
    return data



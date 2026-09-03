"""爬虫底层工具：发HTTP请求、解析标题和正文"""
import re
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

# 常见噪音容器的 class / id 特征（侧边栏、相关推荐、评论、分享、面包屑、目录、
# 订阅、二维码、翻页、广告等）。按「分词后整词命中」判定，避免子串误删
# （如 protocol 含 toc、shared 含 share 这类）；「合伙人/引用/历史」这类
# 语义噪音没有稳定的 CSS 特征，交给 AI 清洗兜底。
_NOISE_TOKENS = {
    # 侧边/导航/面包屑
    "sidebar", "sidenav", "sidemenu",
    "breadcrumb", "breadcrumbs",
    # 翻页
    "pagination", "pager",
    # 相关/推荐/热门
    "related", "relatedposts", "relatedarticles", "hotarticles",
    "recommend", "recommendation", "recommendations", "recommended",
    # 评论/讨论
    "comment", "comments", "discussion", "replies",
    # 分享/社交
    "share", "sharing", "social", "socialshare", "sociallinks",
    # 订阅/关注
    "newsletter", "subscribe", "subscription",
    # 广告
    "advertisement", "sponsor", "sponsored", "adsense",
    # 杂项
    "cookie", "cookies", "qrcode", "wechat",
    # 目录
    "toc", "tableofcontents",
}
# 复合短语（分词后用空格拼回再匹配，兼容 -/_/空格 分隔）
_NOISE_PHRASE_RE = re.compile(
    r"(table of contents|related (post|article|reading)s?|author bio|"
    r"about (the )?author|edit history|revision history|change ?log)",
    re.I,
)


def _is_noise_element(el) -> bool:
    """按 class/id 分词后整词/短语命中判断是否为噪音容器"""
    parts = list(el.get("class") or [])
    if el.get("id"):
        parts.append(el.get("id"))
    tokens = []
    for p in parts:
        tokens.extend(re.split(r"[^a-zA-Z0-9]+", p))
    tokens = [t.lower() for t in tokens if t]
    if not tokens:
        return False
    for t in tokens:
        if t in _NOISE_TOKENS:
            return True
    return bool(_NOISE_PHRASE_RE.search(" ".join(tokens)))

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
    # 去掉脚本、样式、导航、页脚等非正文标签
    if main:
        for tag in main.find_all(
            ["script", "style", "nav", "footer", "header", "aside",
             "form", "button", "iframe", "noscript", "svg", "canvas"]
        ):
            tag.decompose()
        # 再去掉常见噪音容器（侧边栏/相关推荐/评论/分享/面包屑/目录等）
        # 按 class/id 分词整词匹配；先收集再删，避免边遍历边删的问题
        to_remove = []
        for el in main.find_all(True):
            if el is main:
                continue
            if _is_noise_element(el):
                to_remove.append(el)
        for el in to_remove:
            if el.find_parent() is not None:  # 父节点可能已被先行删除
                try:
                    el.decompose()
                except Exception:  # noqa: BLE001
                    pass
        # 代码块保真：<pre>（含嵌套 <code>）转成 ``` 围栏文本再提取，
        # 前端知识详情按围栏/启发式渲染时代码才有高亮排版。
        # 正文自带 ``` 的极端情况跳过，避免围栏嵌套破坏渲染
        for pre in main.find_all("pre"):
            # get_text() 必须不带分隔符：语法高亮的 <pre> 里每个 token 被
            # <span> 包裹，若用 get_text("\n") 会把 token 逐个换行碎裂
            # （rdb / := / redis / . / NewClient 一行一个）；
            # 代码的真实换行在源码里是文本节点，get_text() 原样保留。
            code_text = pre.get_text().strip("\n")
            if code_text and "```" not in code_text:
                pre.replace_with(f"\n```\n{code_text}\n```\n")
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



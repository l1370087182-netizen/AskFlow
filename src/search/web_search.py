"""联网搜索补爬：AI 生成检索词 → 搜索引擎（博查）→ LLM 过滤 → 派生爬取。

设计定位（设计文档 §联网补爬）：
- 「找什么页」的智能部分（生成 query / 过滤候选）在本模块，全部走用户自己的模型
- 「爬取入库」复用既有 CRAWL 链路（producer：清洗→upsert→向量化→质检）
- 触发点两处：对话检索低于阈值（知识库无资料）/ 任务板缺资料主题（planner）
- 异步不阻塞：本模块只负责「搜索 + 选页」，选出的页打包成 CRAWL 子任务入
  任务引擎，由 ProducerAgent 并行消费；提交接口 202 即返
- 全程降级：无模型/无密钥/活跃达上限/搜索失败/过滤无结果 → 返回空或 None，
  调用方照常走「无资料」分支，绝不阻断对话与任务板
"""
import hashlib
import json
import logging
import re

import httpx

from core.config import settings
from search.prompts import FILTER_PROMPT, QUERY_GEN_PROMPT

logger = logging.getLogger(__name__)

# 博查 web-search 端点（SEARCH_BASE_URL 可覆盖，便于接 mock 联调）
BOCHA_SEARCH_URL = "https://api.bochaai.com/v1/web-search"
SEARCH_TIMEOUT_SEC = 20.0

# 单用户同时进行的联网检索任务上限（活跃：排队+执行）
MAX_ACTIVE_SEARCHES = 2


class WebSearchError(Exception):
    """联网检索失败（无密钥/请求失败/返回异常），调用方降级处理"""


# ---------- 博查搜索 ----------

def search_web(query: str, count: int = 8) -> list[dict]:
    """调博查 web-search，返回 [{title, url, snippet}]。

    :raises WebSearchError: 未配置密钥或请求失败（调用方降级，不抛给用户）
    """
    key = settings.SEARCH_API_KEY.strip()
    if not key:
        raise WebSearchError("未配置 SEARCH_API_KEY，联网检索不可用")
    base = settings.SEARCH_BASE_URL.strip() or BOCHA_SEARCH_URL
    try:
        resp = httpx.post(
            base,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "count": max(1, min(50, count)),
                "summary": True,
            },
            timeout=SEARCH_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 —— 网络/解析失败统一降级
        raise WebSearchError(f"搜索请求失败：{e}") from e

    # 博查返回结构：{"data": {"webPages": {"value": [{name,url,snippet,summary}...]}}}
    try:
        value = payload["data"]["webPages"]["value"]
    except (KeyError, TypeError):
        raise WebSearchError("搜索返回结构异常")
    out = []
    for item in value or []:
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        snippet = item.get("summary") or item.get("snippet") or ""
        out.append({
            "title": str(item.get("name", "")).strip(),
            "url": url,
            "snippet": str(snippet).strip(),
        })
    return out


# ---------- LLM：生成检索词 / 过滤候选 ----------

def _parse_json(raw: str) -> dict | None:
    """宽容解析模型 JSON（剥围栏/截花括号），失败返回 None"""
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    candidates = [t]
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        candidates.append(t[start : end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def generate_queries(llm, topic: str, goal: str = "") -> list[str]:
    """主题 → 1~2 条检索词；解析失败回退用主题本身

    :param goal: 学习目标全景（任务板拆解场景），有它检索词更贴用户真实意图
    """
    prompt = QUERY_GEN_PROMPT + f"\n\n【学习主题】{topic}"
    if goal.strip():
        prompt += f"\n【学习目标全景】{goal.strip()}（子题只是其中一角，检索词服务于整体目标）"
    try:
        raw = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        data = _parse_json(raw)
        qs = data.get("queries") if data else None
        if isinstance(qs, list):
            out = [str(q).strip() for q in qs if str(q).strip()]
            if out:
                return out[:2]
    except Exception as e:  # noqa: BLE001 —— 生成失败用主题兜底
        logger.warning("[web-search] 生成检索词失败，用主题直搜：%s", e)
    return [topic]


def _wants_papers(goal: str) -> bool:
    """学习目标里明确点名要论文/学术资料时，才放行论文摘要页"""
    return bool(re.search(r"论文|文献|学术|研究|paper|arxiv", goal, re.IGNORECASE))


def _is_paper_page(url: str) -> bool:
    """URL 层面识别论文摘要页 / PDF（代码层硬拦，不依赖模型自觉）"""
    u = (url or "").lower()
    if u.endswith(".pdf"):
        return True
    return bool(re.search(r"arxiv\.org/(abs|pdf)|biorxiv\.org|ssrn\.com", u))


def filter_candidates(
    llm, db, uid: int, topic: str, candidates: list[dict], goal: str = ""
) -> list[dict]:
    """LLM 按编号挑选有价值页面，逐条做安全与去重校验，返回 [{url, title}]。

    :param goal: 学习目标全景（任务板拆解场景），透传给筛选提示词，
                 让模型以目标而非子题字面判断相关性
    校验：SSRF（validate_public_url）+ 论文摘要页/PDF 代码层硬拦 +
    与本人/全局知识库已有 URL 去重，不合格的静默剔除；
    全部被剔 → 返回空（调用方走「无资料」）。
    """
    from DAO.knowledge_dao import KnowledgeDAO
    from service.knowledge_service import validate_public_url

    if not candidates:
        return []
    # 编号呈现，让模型按序号选，避免改坏网址
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. {c.get('title', '')}\n   摘要：{c.get('snippet', '')[:120]}\n   网址：{c.get('url', '')}")
    goal_line = (
        f"\n\n学习目标全景：{goal.strip()}（子题只是其中一角，与目标明显无关的不要）"
        if goal.strip()
        else ""
    )
    prompt = FILTER_PROMPT.format(
        topic=topic,
        goal_line=goal_line,
        max_keep=settings.SEARCH_MAX_KEEP,
        candidates="\n\n".join(lines),
    )
    try:
        raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        data = _parse_json(raw)
        keep = data.get("keep") if data else None
    except Exception as e:  # noqa: BLE001 —— 过滤失败视为无候选
        logger.warning("[web-search] 候选过滤失败：%s", e)
        return []
    if not isinstance(keep, list):
        return []

    kb_dao = KnowledgeDAO(db)
    chosen: list[dict] = []
    seen_urls: set[str] = set()
    for item in keep:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", 0)) - 1
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(candidates):
            continue
        cand = candidates[idx]
        url = cand.get("url", "")
        if not url or url in seen_urls:
            continue
        # 论文摘要页/PDF 代码层硬拦（学习目标明确要论文才放行）：
        # 摘要页单看「内容充实」，规则/LLM 质检都拦不住，但对学习没价值
        if _is_paper_page(url) and not _wants_papers(goal):
            logger.info("[web-search] 剔除论文/PDF 页：%s", url)
            continue
        try:
            url = validate_public_url(url)
        except ValueError:
            continue  # SSRF/非法：剔除
        # 与知识库已有（本人 + 全局）去重，避免重复爬
        if kb_dao.get_by_url(url, user_id=uid) or kb_dao.get_by_url(url, user_id=0):
            continue
        seen_urls.add(url)
        chosen.append({"url": url, "title": cand.get("title", "") or url})
        if len(chosen) >= settings.SEARCH_MAX_KEEP:
            break
    return chosen


def find_pages(db, llm, uid: int, topic: str) -> list[dict]:
    """编排：生成检索词 → 博查搜索（合并去重）→ LLM 过滤。

    :return: [{url, title}]；任何环节失败或无结果返回 []
    """
    queries = generate_queries(llm, topic)
    candidates: list[dict] = []
    seen: set[str] = set()
    for q in queries:
        try:
            hits = search_web(q, count=settings.SEARCH_MAX_RESULTS)
        except WebSearchError as e:
            logger.warning("[web-search] 检索词「%s」搜索失败：%s", q, e)
            continue
        for h in hits:
            if h["url"] not in seen:
                seen.add(h["url"])
                candidates.append(h)
    if not candidates:
        return []
    return filter_candidates(llm, db, uid, topic, candidates)


# ---------- 提交联网检索任务（异步入口） ----------

def submit_web_search(db, uid: int, topic: str, source: str, goal: str = "") -> str | None:
    """提交联网检索补爬任务（kind=web_search，searcher 异步消费）。

    :param goal: 学习目标全景（任务板拆解场景），随 payload 透传给检索/筛选
    静默降级返回 None：无用户模型 / 未配置密钥 / 活跃检索达上限。
    :return: web_search 任务 id（子题/对话用它跟踪整条链路）
    """
    from DAO.agent_task_dao import AgentTaskDAO
    from generation.llm import build_llm_for_user
    from model.AgentTaskModel import TaskKind

    topic = (topic or "").strip()
    if not topic:
        return None
    try:
        if not settings.SEARCH_API_KEY.strip():
            return None  # 未配置密钥：功能整体关闭，静默
        if build_llm_for_user(db, uid) is None:
            return None  # 无个人模型：与爬取同口径
        dao = AgentTaskDAO(db)
        if dao.count_active(uid, TaskKind.WEB_SEARCH) >= MAX_ACTIVE_SEARCHES:
            return None  # 活跃检索达上限：放弃本次补爬
        task = dao.create(
            kind=TaskKind.WEB_SEARCH,
            user_id=uid,
            payload={"topic": topic, "source": source, "goal": (goal or "").strip()},
            agent="api",
        )
        logger.info("[web-search] 已提交检索任务 %s（topic=%s source=%s）", task.id, topic, source)
        return task.id
    except Exception as e:  # noqa: BLE001 —— 提交失败静默降级，不阻断主流程
        logger.warning("[web-search] 提交检索任务失败（静默降级）：%s", e)
        return None


# 供测试/联调引用的哈希（保持键命名稳定）
def dedup_key(uid: int, topic: str) -> str:
    return f"kb:websearch:dedup:{uid}:{hashlib.sha1(topic.encode('utf-8')).hexdigest()}"

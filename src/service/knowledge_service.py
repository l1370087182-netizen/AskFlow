"""个人知识库服务：手工添加/编辑/删除 + 整站浅爬任务（Redis 状态 + 后台线程消费）。

归属约定（与 knowledge.user_id 对齐）：
- 手工条目占位 source_url = manual://{uid}/{16位hex}，source_type="personal"
- 爬取条目 source_url = 真实页面 URL，source_type="personal"
- 全部操作只影响 user_id=提交者 的行；全局语料（user_id=0）只读

爬取任务状态存 Redis（后端重启不丢进度），后端进程内单条常驻后台线程消费：
- 状态键   kb:crawl:task:{task_id}   TTL 7 天，每页整体覆写
- 活跃键   kb:crawl:active:{uid}     单用户单活跃任务，TTL 30 分钟，终态删
- 消费队列 kb:crawl:queue            Redis List，提交 rpush、线程 blpop
- 进程锁   kb:crawl:worker           SET NX EX 30，多进程部署的保险
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from socket import getaddrinfo
from urllib.parse import urlparse

import httpx
import redis
from bs4 import BeautifulSoup

from core.config import settings
from database.session import SessionLocal
from DAO.knowledge_dao import KnowledgeDAO
from generation.llm import build_llm_for_user
from generation.prompts import AI_ADD_SYSTEM_PROMPT, CLEAN_SYSTEM_PROMPT
from milvus.ingestion.pipeline import IngestionPipeline
from model.KnowledgeModel import KnowledgeModel
from spider.shallow_crawler import ShallowCrawler
from spider.tech_spider import TechSpider

logger = logging.getLogger(__name__)

# ---------- 常量 ----------

# 手工条目占位 URL 前缀（详情弹窗据此不渲染超链接）
MANUAL_SCHEME = "manual://"

# AI 清洗：输入截断阈值与最小可信输出长度
CLEAN_INPUT_LIMIT = 12000
CLEAN_MIN_OUTPUT = 100

# 拒答识别：模型不肯清洗时的典型开头（命中→回退原文）
REFUSAL_MARKERS = (
    "我无法", "我不能", "我不能协助", "抱歉，", "对不起，", "很抱歉",
    "作为AI", "作为 AI", "作为一个语言模型", "作为语言模型",
    "I cannot", "I can't", "I'm sorry", "I am sorry", "I apologize",
    "As an AI", "As a language model",
)

# ---- Redis 键与 TTL ----
TASK_KEY = "kb:crawl:task:{task_id}"     # 任务状态（JSON 整体覆写）
ACTIVE_KEY = "kb:crawl:active:{uid}"     # 单用户活跃任务指针
QUEUE_KEY = "kb:crawl:queue"             # 待消费任务队列（List）
WORKER_KEY = "kb:crawl:worker"           # 多进程互斥锁（SET NX EX）

TASK_TTL_SEC = 7 * 24 * 3600             # 任务状态保留 7 天
ACTIVE_TTL_SEC = 30 * 60                 # 活跃键兜底 30 分钟（正常终态即删）
HEARTBEAT_TIMEOUT_SEC = 10 * 60          # heartbeat 超时 → 对外呈现 failed
WORKER_LOCK_SEC = 30                     # worker 锁续期周期

# 本实例标识（多进程抢锁时区分持有者）
_WORKER_INSTANCE = f"{socket.gethostname()}:{os.getpid()}"


class CrawlSubmitError(Exception):
    """提交爬取任务失败，携带 HTTP 状态码（400=参数/安全/未配置，409=已有活跃任务）"""

    def __init__(self, status_code: int, message: str, task_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.task_id = task_id


def _redis() -> redis.Redis:
    """Redis 客户端（与爬虫/卡片模块同一套连接配置）"""
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )


# ---------- 手工条目 ----------

def manual_source_url(uid: int) -> str:
    """手工条目的行级唯一键：manual://{uid}/{16位随机hex}

    复合唯一约束 (user_id, source_url) 下，每条手工记录都要有唯一键，
    随机后缀保证同一用户可添加任意多条同名/同内容知识。
    """
    return f"{MANUAL_SCHEME}{int(uid)}/{secrets.token_hex(8)}"


def add_manual(
    db, uid: int, *, title: str, content: str, category: str
) -> KnowledgeModel:
    """手工添加个人知识并同步向量化入库（手工添加不走 AI 清洗）。

    向量化失败不回滚条目：status=2 已回写，前端提示「已保存但向量化失败」。
    """
    row = KnowledgeModel(
        title=title,
        content=content,
        source_url=manual_source_url(uid),
        category=category or "general",
        source_type="personal",
        status=KnowledgeModel.STATUS_PENDING,
        user_id=uid,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    try:
        IngestionPipeline(db).ingest_row(row)
    except Exception:  # noqa: BLE001 —— 条目已保存，向量化失败由 status=2 呈现
        logger.exception("[kb-personal] 手工条目 id=%s 向量化失败", row.id)
    db.refresh(row)
    return row


# ---------- AI 清洗 ----------

def _is_refusal(text: str) -> bool:
    """输出开头命中拒答话术 → 视为垃圾输出"""
    head = text[:120]
    return any(marker in head for marker in REFUSAL_MARKERS)


def clean_page_content(llm, title: str, content: str) -> tuple[str, bool]:
    """AI 清洗单页正文，返回 (入库正文, 是否清洗成功)。

    逐页独立降级：请求异常/输出为空/输出过短/拒答 → 回退原文（cleaned=False），
    宁可留噪音也不丢内容。温度 0.2 非流式。
    """
    truncated = len(content) > CLEAN_INPUT_LIMIT
    body = content[:CLEAN_INPUT_LIMIT]
    user_msg = f"页面标题：{title}\n\n原始正文：\n{body}"
    if truncated:
        user_msg += "\n\n（以下为截断内容）"
    try:
        out = llm.chat(
            [
                {"role": "system", "content": CLEAN_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        ).strip()
    except Exception as e:  # noqa: BLE001 —— 清洗失败降级为原文
        logger.warning("[kb-clean] AI 清洗失败，回退原文：%s", e)
        return content, False
    if not out or len(out) < CLEAN_MIN_OUTPUT or _is_refusal(out):
        return content, False
    return out, True


# ---------- SSRF 防护 ----------

def validate_public_url(url: str) -> str:
    """提交端点的目标地址安全检查，通过则返回规范化后的 URL。

    只放行 http/https；解析主机名后逐个检查 IP，
    拒绝内网/环回/链路本地/保留/组播/未指定地址，防止借爬虫打内网。
    """
    clean = url.strip()
    parsed = urlparse(clean)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 地址")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")

    try:
        infos = getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"主机无法解析：{host}") from e

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise ValueError("地址解析异常")
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("不允许抓取内网/本机地址")
    return clean


# ---------- 爬取任务：提交与查询 ----------

def _save_task(r: redis.Redis, state: dict) -> None:
    """任务状态整体覆写（每页一次），TTL 7 天"""
    r.set(
        TASK_KEY.format(task_id=state["task_id"]),
        json.dumps(state, ensure_ascii=False),
        ex=TASK_TTL_SEC,
    )


def _is_alive(state: dict) -> bool:
    """任务是否仍在有效推进：非终态且 heartbeat 未超时"""
    if state["status"] not in ("pending", "running"):
        return False
    return time.time() - state.get("heartbeat", 0) <= HEARTBEAT_TIMEOUT_SEC


def submit_crawl(
    db, *, uid: int, url: str, category: str, max_pages: int
) -> str:
    """提交整站浅爬任务，返回 task_id。

    :raises CrawlSubmitError: 400（未配置模型/SSRF/URL 非法）或 409（已有活跃任务）
    """
    # 1) 清洗必须用用户自己的模型；未配置直接拒绝（不进队列）
    if build_llm_for_user(db, uid) is None:
        raise CrawlSubmitError(
            400,
            "尚未配置个人大模型，请先到「对话学习」页 ⚙️ 模型配置中保存模型后再提交爬取",
        )

    # 2) SSRF / 协议检查
    try:
        url = validate_public_url(url)
    except ValueError as e:
        raise CrawlSubmitError(400, f"URL 不可用：{e}") from e

    r = _redis()

    # 3) 单用户单活跃任务；悬挂（心跳超时）或已终态的旧任务不再拦截
    active_id = r.get(ACTIVE_KEY.format(uid=uid))
    if active_id:
        raw = r.get(TASK_KEY.format(task_id=active_id))
        if raw:
            active_state = json.loads(raw)
            if _is_alive(active_state):
                raise CrawlSubmitError(
                    409, "已有一个爬取任务正在进行，请等待其完成", task_id=active_id
                )

    # 4) 建任务：状态落 Redis → 活跃键 → 入队
    task_id = secrets.token_urlsafe(12)
    state = {
        "task_id": task_id,
        "uid": uid,
        "url": url,
        "category": category or "general",
        "max_pages": max_pages,
        "status": "pending",
        "done_pages": 0,
        "failed_pages": 0,
        "skipped_pages": 0,
        "current_url": "",
        "pages": [],
        "error": "",
        "heartbeat": time.time(),
        "created_at": time.time(),
        "finished_at": 0.0,
    }
    pipe = r.pipeline()
    pipe.set(TASK_KEY.format(task_id=task_id), json.dumps(state, ensure_ascii=False), ex=TASK_TTL_SEC)
    pipe.set(ACTIVE_KEY.format(uid=uid), task_id, ex=ACTIVE_TTL_SEC)
    pipe.rpush(QUEUE_KEY, task_id)
    pipe.execute()
    logger.info("[kb-crawl] 任务已提交：uid=%s url=%s max_pages=%s", uid, url, max_pages)
    return task_id


def get_crawl_task(uid: int, task_id: str) -> dict | None:
    """读取任务进度；不存在/非本人返回 None（控制器统一 404，隐藏存在性）。

    悬挂判定：非终态但 heartbeat 超 10 分钟 → 对外呈现 failed。
    """
    raw = _redis().get(TASK_KEY.format(task_id=task_id))
    if not raw:
        return None
    state = json.loads(raw)
    if state.get("uid") != uid:
        return None
    if state["status"] in ("pending", "running") and not _is_alive(state):
        state["status"] = "failed"
        state["error"] = state.get("error") or "任务超时（工作线程心跳丢失），已标记失败"
    return state


# ---------- 后台消费线程 ----------

_worker_started = False
_worker_start_lock = threading.Lock()


def start_crawl_worker() -> None:
    """幂等拉起常驻后台消费线程（main.py create_app 时调用）。

    daemon=True：随后端进程退出；模块级标记保证重复调用只起一条线程。
    注意：uvicorn --reload 会起多进程，勿在开发模式使用本功能。
    """
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        _worker_started = True
        t = threading.Thread(target=_worker_loop, name="kb-crawl-worker", daemon=True)
        t.start()
        logger.info("[kb-crawl] 后台消费线程已启动（%s）", _WORKER_INSTANCE)


def _worker_loop() -> None:
    """常驻循环：blpop 取任务 → 执行；任何异常都吞掉继续，线程不能死

    复用同一个带连接池的客户端（redis-py 单条命令级自动重连）。
    """
    r = _redis()
    while True:
        try:
            item = r.blpop(QUEUE_KEY, timeout=5)
            if not item:
                continue
            _, task_id = item
            _run_task(task_id)
        except Exception:  # noqa: BLE001 —— 循环本体绝不上抛
            logger.exception("[kb-crawl] 消费循环异常")
            time.sleep(2)


def _acquire_worker_lock(r: redis.Redis) -> bool:
    """多进程保险：SET NX EX 30 抢锁，抢到才能执行任务"""
    return bool(r.set(WORKER_KEY, _WORKER_INSTANCE, nx=True, ex=WORKER_LOCK_SEC))


def _renew_worker_lock(r: redis.Redis) -> None:
    if r.get(WORKER_KEY) == _WORKER_INSTANCE:
        r.expire(WORKER_KEY, WORKER_LOCK_SEC)


def _release_worker_lock(r: redis.Redis) -> None:
    try:
        if r.get(WORKER_KEY) == _WORKER_INSTANCE:
            r.delete(WORKER_KEY)
    except Exception:  # noqa: BLE001
        logger.exception("[kb-crawl] 释放 worker 锁失败")


def _run_task(task_id: str) -> None:
    """执行单个爬取任务：逐页 抓取→校验→AI清洗→upsert→即时向量化。

    独立 SessionLocal（后台线程不能用请求级 Session）；
    无论成败，finally 里必写终态、释放活跃键与 worker 锁。
    """
    r = _redis()
    task_key = TASK_KEY.format(task_id=task_id)

    raw = r.get(task_key)
    if not raw:
        return  # 任务已过期/不存在
    state = json.loads(raw)
    uid = state["uid"]
    active_key = ACTIVE_KEY.format(uid=uid)

    # 多进程保险：抢不到锁就放回队列，稍后再试
    if not _acquire_worker_lock(r):
        logger.warning("[kb-crawl] 未抢到 worker 锁，任务 %s 重新入队", task_id)
        r.rpush(QUEUE_KEY, task_id)
        time.sleep(3)
        return

    db = SessionLocal()
    try:
        # 双保险：提交时查过一次，执行前再查（用户可能中途清空配置）
        llm = build_llm_for_user(db, uid)
        if llm is None:
            state["status"] = "failed"
            state["error"] = "未配置个人大模型，请先到「对话学习」页 ⚙️ 配置模型后再爬取"
            return

        state["status"] = "running"
        state["heartbeat"] = time.time()
        _save_task(r, state)

        crawler = ShallowCrawler(max_pages=state["max_pages"])
        for page in crawler.iter_pages(state["url"]):
            state["current_url"] = page["url"]
            state["heartbeat"] = time.time()
            _renew_worker_lock(r)

            if not page["ok"]:
                # 抓取失败：记入失败页，逐页降级继续
                state["failed_pages"] += 1
                state["pages"].append(
                    {
                        "url": page["url"],
                        "ok": False,
                        "cleaned": False,
                        "knowledge_id": None,
                        "error": page.get("error", "抓取失败"),
                    }
                )
                _save_task(r, state)
                continue

            if not TechSpider.is_valid_article(page):
                # 正文太短（导航页/空页）：无知识价值，跳过不入库
                state["skipped_pages"] += 1
                _save_task(r, state)
                continue

            # AI 清洗（失败自动回退原文）→ upsert → 即时向量化
            title = page.get("title") or page["url"]
            cleaned_text, cleaned = clean_page_content(llm, title, page["content"])
            row = KnowledgeDAO(db).upsert(
                title=title,
                content=cleaned_text,
                source_url=page["url"],
                category=state["category"],
                source_type="personal",
                user_id=uid,
            )
            knowledge_id = row.id if row else None
            if row is not None and row.status == KnowledgeModel.STATUS_PENDING:
                try:
                    IngestionPipeline(db).ingest_row(row)
                except Exception:  # noqa: BLE001 —— 单页向量化失败不中断任务
                    logger.exception("[kb-crawl] 页面 %s 向量化失败", page["url"])

            state["done_pages"] += 1
            state["pages"].append(
                {
                    "url": page["url"],
                    "ok": True,
                    "cleaned": cleaned,
                    "knowledge_id": knowledge_id,
                    "error": "",
                }
            )
            _save_task(r, state)

        # 终态判定：全部成功=done；有失败但有成功=partial；颗粒无收=failed
        if state["done_pages"] > 0 and state["failed_pages"] == 0:
            state["status"] = "done"
        elif state["done_pages"] > 0:
            state["status"] = "partial"
        else:
            state["status"] = "failed"
            first_err = next((p["error"] for p in state["pages"] if p.get("error")), "")
            state["error"] = first_err or "未能爬到任何有效页面"
    except Exception as e:  # noqa: BLE001 —— 总兜底：任务置 failed
        logger.exception("[kb-crawl] 任务 %s 异常", task_id)
        state["status"] = "failed"
        state["error"] = f"任务执行异常：{e}"
    finally:
        # 必达：写终态 + 释放活跃键（仅当仍指向本任务）+ 释放 worker 锁
        state["finished_at"] = time.time()
        try:
            _save_task(r, state)
            if r.get(active_key) == task_id:
                r.delete(active_key)
        except Exception:  # noqa: BLE001
            logger.exception("[kb-crawl] 任务 %s 收尾失败", task_id)
        _release_worker_lock(r)
        db.close()
        logger.info(
            "[kb-crawl] 任务 %s 结束：%s（成功 %s / 失败 %s / 跳过 %s）",
            task_id, state["status"],
            state["done_pages"], state["failed_pages"], state["skipped_pages"],
        )


# ---------- AI 添加（对话式定题 → 自动爬取） ----------

# 对话历史上限（每轮全量回传，截断防 token 爆炸）
AI_ADD_MAX_HISTORY = 12


def _parse_ai_add_reply(text: str) -> dict | None:
    """解析模型的 AI 添加回复为 dict；剥代码围栏、容忍前后杂文，失败返回 None"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(t[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def probe_url(url: str, timeout: float = 15.0) -> tuple[bool, str, str]:
    """探测目标地址可达性，返回 (ok, 最终地址(跟随重定向), 页面标题)。

    模型给的 URL 可能失效/重定向，先探一把再入队，
    避免用户等一个注定失败的任务；标题用于前端展示「将爬取：XXX」。
    """
    try:
        resp = httpx.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RAG-KB/1.0)"},
        )
        resp.raise_for_status()
        # 只取头部一段找 title，避免大页面全量进 BeautifulSoup
        soup = BeautifulSoup(resp.text[:20000], "lxml")
        title = soup.title.get_text(strip=True) if soup.title else ""
        return True, str(resp.url), title
    except Exception as e:  # noqa: BLE001 —— 探测失败不抛，降级为追问
        logger.info("[kb-ai-add] 探测失败 %s：%s", url, e)
        return False, url, ""


def ai_add_chat(db, uid: int, messages: list[dict]) -> dict:
    """AI 添加：用户说想学什么 → 模型判断（追问/纠错/直接爬）→ 提交爬取任务。

    :return: {"action": "ask"|"crawl", "message", "proposal", "task_id"}
    :raises CrawlSubmitError: 400（未配置模型/空输入）或 502（模型调用失败）
    """
    llm = build_llm_for_user(db, uid)
    if llm is None:
        raise CrawlSubmitError(
            400, "尚未配置个人大模型，请先到「对话学习」页 ⚙️ 模型配置中保存模型后再用 AI 添加"
        )

    history = [
        {"role": m["role"], "content": m["content"][:1000]}
        for m in messages[-AI_ADD_MAX_HISTORY:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not history:
        raise CrawlSubmitError(400, "请先告诉我你想了解什么")

    try:
        reply = llm.chat(
            [{"role": "system", "content": AI_ADD_SYSTEM_PROMPT}, *history],
            temperature=0.3,
        )
    except Exception as e:  # noqa: BLE001 —— 模型调用失败如实上报
        raise CrawlSubmitError(502, f"模型调用失败：{e}") from e

    data = _parse_ai_add_reply(reply)
    if not data or data.get("action") not in ("ask", "crawl"):
        # 解析失败：把原文当作追问返回，对话不中断
        return {
            "action": "ask",
            "message": reply.strip()[:500] or "请再补充一点信息",
            "proposal": None,
            "task_id": None,
        }

    if data["action"] == "ask":
        return {
            "action": "ask",
            "message": str(data.get("message", "")).strip()[:500] or "请再补充一点信息",
            "proposal": None,
            "task_id": None,
        }

    # ---- action == crawl：校验 → 探活 → 提交任务 ----
    url = str(data.get("url", "")).strip()
    try:
        url = validate_public_url(url)
    except ValueError as e:
        return {
            "action": "ask",
            "message": f"模型给出的地址不可用（{e}），请换个说法描述你想学的内容",
            "proposal": None,
            "task_id": None,
        }

    ok, final_url, probed_title = probe_url(url)
    if not ok:
        return {
            "action": "ask",
            "message": "我试着访问了模型推荐的文档地址，但它没有响应。请换个说法，或把主题描述得更具体一些",
            "proposal": None,
            "task_id": None,
        }

    try:
        max_pages = max(1, min(20, int(data.get("max_pages", 10))))
    except (TypeError, ValueError):
        max_pages = 10
    category = (
        str(data.get("category", "") or "general").strip().lower()[:128] or "general"
    )
    title = str(data.get("title", "") or probed_title or final_url)[:512]

    try:
        task_id = submit_crawl(
            db, uid=uid, url=final_url, category=category, max_pages=max_pages
        )
    except CrawlSubmitError as e:
        # 已有活跃任务（409）：把 task_id 透传出去，前端可直接跳进度面板；
        # 其余错误转为追问，不打断对话
        return {
            "action": "ask",
            "message": e.message,
            "proposal": None,
            "task_id": e.task_id,
        }

    return {
        "action": "crawl",
        "message": str(data.get("message", "")).strip()[:500]
        or f"开始爬取：{title}",
        "proposal": {
            "url": final_url,
            "title": title,
            "category": category,
            "max_pages": max_pages,
        },
        "task_id": task_id,
    }

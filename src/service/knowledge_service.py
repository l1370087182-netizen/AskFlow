"""个人知识库服务：手工添加/编辑/删除 + 整站浅爬任务（Redis 状态 + 后台线程消费）。

归属约定（与 knowledge.user_id 对齐）：
- 手工条目占位 source_url = manual://{uid}/{16位hex}，source_type="personal"
- 爬取条目 source_url = 真实页面 URL，source_type="personal"
- 全部操作只影响 user_id=提交者 的行；全局语料（user_id=0）只读

爬取任务状态存 Redis（后端重启不丢进度），后端进程内常驻调度线程 + 线程池并行消费：
- 状态键   kb:crawl:task:{task_id}   TTL 7 天，每页整体覆写
- 活跃集合 kb:crawl:active:{uid}     单用户活跃任务 SET（并行化后允许多任务），
                                     终态移除；读取时剪枝过期/终态成员
- 消费队列 kb:crawl:queue            Redis List，提交 rpush、调度线程 blpop 分发
- 执行集合 kb:crawl:inflight         已出队未终态的任务 id 集合；任务被出队即
                                     从队列移除，进程若此时死掉任务就悬空了，
                                     靠它在启动时自检：心跳超时的重新入队续跑

任务分发依赖 blpop 的原子性（一个任务只会被一个消费者取走），
因此不再有全局执行锁；并行度由 CRAWL_WORKERS 控制。
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
from concurrent.futures import ThreadPoolExecutor
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
ACTIVE_KEY = "kb:crawl:active:{uid}"     # 单用户活跃任务集合（SET，允许多任务）
QUEUE_KEY = "kb:crawl:queue"             # 待消费任务队列（List）
INFLIGHT_KEY = "kb:crawl:inflight"       # 执行中任务 id 集合（出队未终态，中断恢复用）
RECOVER_KEY = "kb:crawl:recover"         # 启动恢复互斥锁（防多进程重复入队）

TASK_TTL_SEC = 7 * 24 * 3600             # 任务状态保留 7 天
HEARTBEAT_TIMEOUT_SEC = 10 * 60          # heartbeat 超时 → 对外呈现 failed
RECOVER_LOCK_SEC = 60                    # 恢复锁有效期（覆盖一次启动自检绰绰有余）

# ---- 并行度与上限 ----
CRAWL_WORKERS = 3                        # 同时执行的爬取任务数（线程池大小）
MAX_ACTIVE_PER_USER = 5                  # 单用户活跃（排队+执行）任务上限，超限 409

# 本实例标识（多进程抢锁时区分持有者）
_WORKER_INSTANCE = f"{socket.gethostname()}:{os.getpid()}"


class CrawlSubmitError(Exception):
    """提交爬取任务失败，携带 HTTP 状态码（400=参数/安全/未配置，409=活跃任务达上限）"""

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


def _heartbeat(r: redis.Redis, state: dict) -> None:
    """刷新心跳并立即持久化。

    在每个阻塞步骤（抓页、AI 清洗、向量化）前后调用，
    把「心跳静止」窗口压到单步耗时级，避免被误判为心跳丢失。
    """
    state["heartbeat"] = time.time()
    _save_task(r, state)


def _is_alive(state: dict) -> bool:
    """任务是否仍在有效推进：非终态且 heartbeat 未超时"""
    if state["status"] not in ("pending", "running"):
        return False
    return time.time() - state.get("heartbeat", 0) <= HEARTBEAT_TIMEOUT_SEC


# ---------- 活跃任务集合（并行化：单用户多任务） ----------

def _active_add(r: redis.Redis, uid: int, task_id: str) -> None:
    try:
        r.sadd(ACTIVE_KEY.format(uid=uid), task_id)
    except redis.ResponseError:
        # 旧版本单值键残留（WRONGTYPE）：删掉重建，旧键 30 分钟 TTL 内自然清完
        r.delete(ACTIVE_KEY.format(uid=uid))
        r.sadd(ACTIVE_KEY.format(uid=uid), task_id)


def _active_remove(r: redis.Redis, uid: int, task_id: str) -> None:
    try:
        r.srem(ACTIVE_KEY.format(uid=uid), task_id)
    except redis.ResponseError:
        r.delete(ACTIVE_KEY.format(uid=uid))


def _active_members(r: redis.Redis, uid: int) -> set:
    try:
        return r.smembers(ACTIVE_KEY.format(uid=uid))
    except redis.ResponseError:
        r.delete(ACTIVE_KEY.format(uid=uid))
        return set()


def _prune_active(r: redis.Redis, uid: int) -> list[dict]:
    """读活跃集合并剪枝：状态过期/已终态的成员移出，返回存活任务状态列表。

    剪枝时机即读取时机（提交查上限、进度恢复面板），无需额外定时器。
    """
    alive = []
    for task_id in _active_members(r, uid):
        state = get_crawl_task(uid, task_id)
        if state is None or state["status"] not in ("pending", "running"):
            _active_remove(r, uid, task_id)
            continue
        alive.append(state)
    return alive


def submit_crawl(
    db, *, uid: int, url: str, category: str, max_pages: int
) -> str:
    """提交整站浅爬任务，返回 task_id。支持多任务并行（上限 MAX_ACTIVE_PER_USER）。

    :raises CrawlSubmitError: 400（未配置模型/SSRF/URL 非法）或 409（活跃任务达上限）
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

    # 3) 活跃任务上限（排队+执行中）；读取时顺带剪枝过期/终态成员
    if len(_prune_active(r, uid)) >= MAX_ACTIVE_PER_USER:
        raise CrawlSubmitError(
            409, f"最多同时进行 {MAX_ACTIVE_PER_USER} 个爬取任务，请等前面的完成后再提交"
        )

    # 4) 建任务：状态落 Redis → 入队 → 活跃集合标记
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
    pipe.rpush(QUEUE_KEY, task_id)
    pipe.execute()
    _active_add(r, uid, task_id)
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


def get_active_crawl_tasks(uid: int) -> list[dict]:
    """用户当前全部进行中的爬取任务（进度面板恢复用）；无活跃任务返回空列表。

    内部走 _prune_active：过期/终态成员顺带剪枝；
    心跳超时的悬挂任务经 get_crawl_task 已置 failed，同样不算活跃。
    """
    return _prune_active(_redis(), uid)


# ---------- 后台消费：调度线程 + 线程池并行执行 ----------

_worker_started = False
_worker_start_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
# Milvus Lite 对线程级并发写无官方保证，进程级单例客户端加锁串行向量化
_ingest_lock = threading.Lock()


def start_crawl_worker() -> None:
    """幂等拉起常驻后台消费（main.py create_app 时调用）：
    一条调度线程 blpop 分发 + CRAWL_WORKERS 条执行线程并行跑任务。

    daemon 线程随后端进程退出；模块级标记保证重复调用只起一套。
    起线程前先自检：上次停机/崩溃中断的任务自动重新入队续跑。
    注意：uvicorn --reload 会起多进程，勿在开发模式使用本功能。
    """
    global _worker_started, _executor
    with _worker_start_lock:
        if _worker_started:
            return
        _worker_started = True
        _recover_interrupted_tasks()
        _executor = ThreadPoolExecutor(
            max_workers=CRAWL_WORKERS, thread_name_prefix="kb-crawl-task"
        )
        t = threading.Thread(target=_worker_loop, name="kb-crawl-dispatch", daemon=True)
        t.start()
        logger.info(
            "[kb-crawl] 调度线程 + %d 条执行线程已启动（%s）", CRAWL_WORKERS, _WORKER_INSTANCE
        )


def _recover_interrupted_tasks() -> None:
    """启动自检：把随上一进程死掉的任务（已出队、非终态、心跳超时）重新入队。

    任务被 blpop 出队后就只存在于执行进程里，进程一死就悬空——
    不恢复的话只能等 10 分钟后被前端看到「心跳丢失」。
    判死口径与 get_crawl_task 一致（心跳超时）；心跳仍新鲜的任务
    （可能另一实例正在跑）不动。NX 锁防多进程同时启动重复入队。
    恢复失败不阻塞 worker 启动。
    """
    try:
        r = _redis()
        if not r.set(RECOVER_KEY, _WORKER_INSTANCE, nx=True, ex=RECOVER_LOCK_SEC):
            return
        for task_id in r.smembers(INFLIGHT_KEY):
            raw = r.get(TASK_KEY.format(task_id=task_id))
            if not raw:
                r.srem(INFLIGHT_KEY, task_id)  # 状态已过期，清残留标记
                continue
            state = json.loads(raw)
            if state["status"] not in ("pending", "running"):
                r.srem(INFLIGHT_KEY, task_id)  # 上进程已正常收尾，只清了标记
                continue
            if not _is_alive(state):
                r.rpush(QUEUE_KEY, task_id)
                r.srem(INFLIGHT_KEY, task_id)
                logger.warning(
                    "[kb-crawl] 检测到中断任务 %s（心跳超时），已重新入队续跑", task_id
                )
            # 心跳仍新鲜：可能另一实例正在执行，不动
    except Exception:  # noqa: BLE001 —— 恢复失败不影响 worker 启动
        logger.exception("[kb-crawl] 中断任务恢复失败")


def _worker_loop() -> None:
    """调度循环：blpop 取任务 → 提交线程池并行执行；任何异常都吞掉继续，线程不能死

    任务归属由 blpop 原子性保证（一个任务只会被取走一次），无需全局执行锁。
    复用同一个带连接池的客户端（redis-py 单条命令级自动重连）。
    """
    r = _redis()
    while True:
        try:
            item = r.blpop(QUEUE_KEY, timeout=5)
            if not item:
                continue
            _, task_id = item
            # 出队即标记执行中：此刻任务已不在队列，进程死掉就靠它启动时恢复
            r.sadd(INFLIGHT_KEY, task_id)
            _executor.submit(_run_task_guarded, task_id)
        except Exception:  # noqa: BLE001 —— 循环本体绝不上抛
            logger.exception("[kb-crawl] 消费调度异常")
            time.sleep(2)


def _run_task_guarded(task_id: str) -> None:
    """线程池执行包装：兜住一切意外异常，防止静默丢任务"""
    try:
        _run_task(task_id)
    except Exception:  # noqa: BLE001
        logger.exception("[kb-crawl] 任务 %s 意外异常", task_id)


def _run_task(task_id: str) -> None:
    """执行单个爬取任务：逐页 抓取→校验→AI清洗→upsert→即时向量化。

    独立 SessionLocal（线程池里每个任务一个，天然隔离）；
    无论成败，finally 里必写终态、移出活跃集合与执行集合。
    """
    r = _redis()
    task_key = TASK_KEY.format(task_id=task_id)

    raw = r.get(task_key)
    if not raw:
        r.srem(INFLIGHT_KEY, task_id)  # 任务已过期，清掉出队标记
        return
    state = json.loads(raw)
    uid = state["uid"]

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
        # 排队期间可能已被剪枝（心跳超时判死），重新挂回活跃集合
        _active_add(r, uid, task_id)

        crawler = ShallowCrawler(max_pages=state["max_pages"])
        for page in crawler.iter_pages(state["url"]):
            state["current_url"] = page["url"]
            _heartbeat(r, state)

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
            # 阻塞步骤前后刷新心跳：清洗走用户模型（≤120s/次）、向量化走 Milvus，
            # 慢调用不该被算进「心跳静止」窗口
            _heartbeat(r, state)
            cleaned_text, cleaned = clean_page_content(llm, title, page["content"])
            _heartbeat(r, state)
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
                _heartbeat(r, state)
                try:
                    # 进程级锁：Milvus Lite 单文件单例，并行任务的向量化串行写
                    with _ingest_lock:
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
        # 必达：写终态 + 移出活跃集合 + 移出执行集合
        state["finished_at"] = time.time()
        try:
            _save_task(r, state)
            _active_remove(r, uid, task_id)
            r.srem(INFLIGHT_KEY, task_id)
        except Exception:  # noqa: BLE001
            logger.exception("[kb-crawl] 任务 %s 收尾失败", task_id)
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
        # 提交失败（活跃任务达上限 409 / 未配置模型 400 等）：
        # 转为追问话术不打断对话；task_id 仅 409 且带值时前端可跳进度面板
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

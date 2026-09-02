"""个人知识库服务：手工添加/编辑/删除 + 整站浅爬任务提交与查询。

归属约定（与 knowledge.user_id 对齐）：
- 手工条目占位 source_url = manual://{uid}/{16位hex}，source_type="personal"
- 爬取条目 source_url = 真实页面 URL，source_type="personal"
- 全部操作只影响 user_id=提交者 的行；全局语料（user_id=0）只读

爬取任务的存储分工（任务引擎改造后）：
- 生命周期 = agent_task 表（数据库是唯一真相源）：提交/认领/重试/终态
  由任务引擎管理（见 agent_engine/ 与 agents/producer.py）
- 逐页进度态 = Redis kb:crawl:task:{task_id}（前端进度面板的实时数据，
  允许丢失——丢了只影响面板展示，不影响任务执行）
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from socket import getaddrinfo
from urllib.parse import urlparse

import httpx
import redis
from bs4 import BeautifulSoup

from DAO.agent_task_dao import AgentTaskDAO
from database.session import SessionLocal
from generation.llm import build_llm_for_user
from generation.prompts import AI_ADD_SYSTEM_PROMPT, CLEAN_SYSTEM_PROMPT
from milvus.ingestion.pipeline import IngestionPipeline
from model.AgentTaskModel import TaskKind, TaskStatus
from model.KnowledgeModel import KnowledgeModel
from util.redis_util import make_redis

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

# ---- 进度态 Redis 键（生命周期在 agent_task 表，这里只存实时进度）----
TASK_KEY = "kb:crawl:task:{task_id}"     # 任务进度态（JSON 整体覆写）
TASK_TTL_SEC = 7 * 24 * 3600             # 进度态保留 7 天

# 前端视图的心跳超时（对外呈现 failed 用；引擎回收阈值见 agent_engine.reaper）
HEARTBEAT_TIMEOUT_SEC = 10 * 60

# 单用户活跃（排队+执行）爬取任务上限
MAX_ACTIVE_PER_USER = 5


class CrawlSubmitError(Exception):
    """提交爬取任务失败，携带 HTTP 状态码（400=参数/安全/未配置，409=活跃任务达上限）"""

    def __init__(self, status_code: int, message: str, task_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.task_id = task_id


def _redis() -> redis.Redis:
    """Redis 客户端（统一工厂：短超时+失败即抛，见 util/redis_util）"""
    return make_redis()


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
    """进度态整体覆写（每页一次），TTL 7 天"""
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
    """提交整站浅爬任务：写入任务引擎（agent_task），由 ProducerAgent 并行消费。

    :return: task_id（= agent_task.id）
    :raises CrawlSubmitError: 400（未配置模型/SSRF/URL 非法）或 409（活跃任务达上限）
    """
    # 1) 清洗必须用用户自己的模型；未配置直接拒绝（不建任务）
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

    # 3) 活跃任务上限（DB 为真相源：pending+in_progress）
    dao = AgentTaskDAO(db)
    if dao.count_active(uid, TaskKind.CRAWL) >= MAX_ACTIVE_PER_USER:
        raise CrawlSubmitError(
            409, f"最多同时进行 {MAX_ACTIVE_PER_USER} 个爬取任务，请等前面的完成后再提交"
        )

    # 4) 建任务（kind=crawl，等待 producer 认领）+ 进度态落 Redis（前端面板）
    task = dao.create(
        kind=TaskKind.CRAWL,
        user_id=uid,
        payload={"url": url, "category": category or "general", "max_pages": max_pages},
    )
    try:
        _save_task(_redis(), {
            "task_id": task.id,
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
        })
    except redis.RedisError as e:
        # 进度态丢失不影响任务执行（只会少面板展示），记日志继续
        logger.warning("[kb-crawl] 进度态写入失败（任务照常执行）：%s", e)
    logger.info(
        "[kb-crawl] 任务已提交：uid=%s url=%s max_pages=%s task=%s",
        uid, url, max_pages, task.id,
    )
    return task.id


def get_crawl_task(uid: int, task_id: str) -> dict | None:
    """读取任务进度；不存在/非本人返回 None（控制器统一 404，隐藏存在性）。

    悬挂判定：非终态但 heartbeat 超 10 分钟 → 对外呈现 failed。
    Redis 不可用时进度态本就不可见，同样按「查不到」降级（不抛 500）。
    """
    try:
        raw = _redis().get(TASK_KEY.format(task_id=task_id))
    except redis.RedisError as e:
        logger.warning("[kb-crawl] 进度查询降级（Redis 不可用）：%s", e)
        return None
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
    """用户当前全部进行中的爬取任务（进度面板恢复用）。

    生命周期以 agent_task 表为真相源（pending+in_progress），
    进度视图取 Redis；排队中（pending）的任务始终算活跃——
    即使进度态心跳陈旧（如超时回收后续跑前），也不误判失败。
    """
    db = SessionLocal()
    try:
        rows = AgentTaskDAO(db).list_by_user(
            uid,
            kind=TaskKind.CRAWL,
            statuses=[TaskStatus.PENDING, TaskStatus.IN_PROGRESS],
        )
    finally:
        db.close()

    try:
        r = _redis()
    except Exception:  # noqa: BLE001 —— Redis 配置异常也不影响列表语义
        r = None

    out = []
    for row in rows:
        state = None
        if r is not None:
            try:
                raw = r.get(TASK_KEY.format(task_id=row.id))
                if raw:
                    state = json.loads(raw)
            except Exception:  # noqa: BLE001
                state = None
        if state is None:
            # 进度态缺失（Redis 重启等）：按 payload 拼 pending 视图，不丢任务
            p = row.payload or {}
            state = {
                "task_id": row.id,
                "uid": uid,
                "url": p.get("url", ""),
                "category": p.get("category", "general"),
                "max_pages": p.get("max_pages", 10),
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
        if row.status == TaskStatus.PENDING:
            state["status"] = "pending"  # 排队（含回收待重跑）以 DB 为准
        elif state["status"] == "running" and not _is_alive(state):
            state["status"] = "failed"
            state["error"] = state.get("error") or "任务超时（心跳丢失），已标记失败"
        out.append(state)
    return out


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

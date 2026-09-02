"""知识库翻译服务：微软 Edge 免密钥翻译接口（实测契约）。

- 接口  POST https://edge.microsoft.com/translate/translatetext?from=<可省>&to=zh-Hans
        body 为 JSON 字符串数组（每项一段文本），响应数组一一对应：
        [{"translations":[{"text":"…"}], "detectedLanguage"?}, …]
- 限制  请求体上限约 50KB（实测 48KB 过 / 50KB 拒），按 30KB 切批留余量
- 语言  from 省略即自动检测（响应带 detectedLanguage）
- 结构  ``` 围栏代码块原样直通（防代码被翻坏）；文本按段落切块，译文结构不丢
- 缓存  Redis 键带内容指纹（内容一变键自动失效，无需在各写路径手动清理）
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import redis

from core.config import settings
from DAO.knowledge_dao import KnowledgeDAO
from model.KnowledgeModel import KnowledgeModel

logger = logging.getLogger(__name__)

# ---- 接口与限制（实测值，见模块 docstring）----
EDGE_TRANSLATE_URL = "https://edge.microsoft.com/translate/translatetext"
DEFAULT_TARGET_LANG = "zh-Hans"
MAX_BATCH_BYTES = 30 * 1024        # 单请求体预算（上限约 50KB，留余量）
MAX_SEGMENT_CHARS = 4000           # 单段长度上限
REQUEST_TIMEOUT = 40.0             # 单请求超时（秒）
BATCH_WORKERS = 4                  # 长文多批并行翻译的并发数
RETRY_TIMES = 1                    # 单批失败重试次数

# ---- 缓存 ----
CACHE_KEY = "kb:trans:{kid}:{fp}"  # 内容指纹入键：内容变更自动换键
CACHE_TTL_SEC = 7 * 24 * 3600


class TranslateError(Exception):
    """翻译失败（接口不可用/超限/解析异常），控制器映射为 502"""


def _redis() -> redis.Redis:
    """Redis 客户端（与爬虫/卡片模块同一套连接配置）"""
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )


# ---------- 文本切分 ----------

def _split_code_fences(text: str) -> list[tuple[bool, str]]:
    """按 ``` 围栏切成 [(是否代码块, 片段), ...]，代码块整体直通不翻译。

    围栏开启前的文本先落盘；未闭合的围栏尾部按代码处理（宁可不翻也不翻坏）。
    """
    segments: list[tuple[bool, str]] = []
    buf: list[str] = []
    in_code = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            if in_code:
                buf.append(line)  # 闭围栏归属代码块
                segments.append((True, "".join(buf)))
                buf = []
                in_code = False
            else:
                if buf:
                    segments.append((False, "".join(buf)))
                    buf = []
                buf.append(line)  # 开围栏归属代码块
                in_code = True
            continue
        buf.append(line)
    if buf:
        segments.append((in_code, "".join(buf)))
    return segments


def _split_text_chunks(text: str) -> list[str]:
    """文本块切翻译段：优先段落边界（空行），超长段落按行聚合、单行再硬切"""
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip("\n")
        if not para.strip():
            continue
        if len(para) <= MAX_SEGMENT_CHARS:
            chunks.append(para)
            continue
        buf = ""
        for line in para.splitlines():
            if len(line) > MAX_SEGMENT_CHARS:
                # 超长行（无换行的巨型段落）直接硬切
                if buf:
                    chunks.append(buf)
                    buf = ""
                for i in range(0, len(line), MAX_SEGMENT_CHARS):
                    chunks.append(line[i : i + MAX_SEGMENT_CHARS])
                continue
            if buf and len(buf) + 1 + len(line) > MAX_SEGMENT_CHARS:
                chunks.append(buf)
                buf = ""
            buf = f"{buf}\n{line}" if buf else line
        if buf:
            chunks.append(buf)
    return chunks


def _make_batches(seg_texts: list[str]) -> list[tuple[int, int]]:
    """按请求体字节预算把段落下标分批，返回 [(start, end), ...]"""
    batches: list[tuple[int, int]] = []
    start, budget = 0, 2  # 2 字节给 [] 括号
    for i, s in enumerate(seg_texts):
        cost = len(s.encode("utf-8")) + 4  # 引号 + 逗号余量
        if start < i and budget + cost > MAX_BATCH_BYTES:
            batches.append((start, i))
            start, budget = i, 2
        budget += cost
    if start < len(seg_texts):
        batches.append((start, len(seg_texts)))
    return batches


def _looks_chinese(sample: str) -> bool:
    """启发式：采样里汉字占字母数比例高 → 视为中文（省一次接口调用）"""
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    alpha = sum(1 for ch in sample if ch.isalpha())
    return alpha > 0 and cjk / alpha >= 0.4


# ---------- 接口调用 ----------

def _translate_batch(batch: list[str], to: str, from_lang: str | None) -> tuple[list[str], str | None]:
    """单批调用，返回 (与输入等长的译文列表, 检测到的源语言)。失败重试一次"""
    params: dict[str, str] = {"to": to}
    if from_lang:
        params["from"] = from_lang
    body = json.dumps(batch, ensure_ascii=False).encode("utf-8")
    last_err: Exception | None = None
    for _ in range(RETRY_TIMES + 1):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                resp = client.post(
                    EDGE_TRANSLATE_URL,
                    params=params,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            texts: list[str] = []
            detected: str | None = None
            for item in data:
                trans = item.get("translations") or []
                texts.append(trans[0].get("text", "") if trans else "")
                if detected is None and item.get("detectedLanguage"):
                    detected = item["detectedLanguage"].get("language")
            if len(texts) != len(batch):
                raise ValueError(f"响应段数 {len(texts)} 与请求段数 {len(batch)} 不一致")
            return texts, detected
        except Exception as e:  # noqa: BLE001 —— 重试；用尽后抛 TranslateError
            last_err = e
            logger.warning("[translate] 批次调用失败（%s），准备重试", e)
    raise TranslateError(f"翻译接口调用失败：{last_err}")


def translate_text(text: str, to: str = DEFAULT_TARGET_LANG, from_lang: str | None = None) -> dict:
    """翻译整段文本，保留代码块与段落结构。

    :return: {"text": 译文, "detected": 检测源语言|None, "same_language": 是否本就中文}
    """
    parts = _split_code_fences(text)
    text_part_idx = [i for i, (is_code, seg) in enumerate(parts) if not is_code and seg.strip()]
    if not text_part_idx:
        return {"text": text, "detected": None, "same_language": False}

    # 全文中文主导 → 原样返回，不花调用
    head_sample = "".join(parts[i][1] for i in text_part_idx)[:800]
    if not from_lang and _looks_chinese(head_sample):
        return {"text": text, "detected": "zh", "same_language": True}

    # 各文本块切段，记录 段 -> 全局下标 的映射
    part_seg_map: dict[int, list[int]] = {}
    all_segments: list[str] = []
    for i in text_part_idx:
        chunks = _split_text_chunks(parts[i][1])
        part_seg_map[i] = list(range(len(all_segments), len(all_segments) + len(chunks)))
        all_segments.extend(chunks)
    if not all_segments:
        return {"text": text, "detected": None, "same_language": False}

    # 按字节预算分批，多批并行请求
    batches = _make_batches(all_segments)
    translations: list[str] = [""] * len(all_segments)
    detected: str | None = None
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        results = list(
            pool.map(
                lambda rng: _translate_batch(all_segments[rng[0] : rng[1]], to, from_lang),
                batches,
            )
        )
    for (start, end), (texts, batch_detected) in zip(batches, results):
        translations[start:end] = texts
        if detected is None:
            detected = batch_detected

    # 逐块拼回：文本块用段落分隔重组，代码块原样
    out: list[str] = []
    for i, (is_code, seg) in enumerate(parts):
        if is_code or not seg.strip() or i not in part_seg_map:
            out.append(seg)
        else:
            out.append("\n\n".join(translations[j] for j in part_seg_map[i]))
    return {"text": "".join(out), "detected": detected, "same_language": False}


# ---------- 知识条目翻译（带指纹缓存）----------

def translate_knowledge(db, uid: int, knowledge_id: int) -> dict | None:
    """翻译单条知识（标题+正文）。归属同详情读路：他人个人条目返回 None。

    :return: {id, detected, same_language, title, content, cached} | None（无权限/不存在）
    """
    row = KnowledgeDAO(db).get_by_db(knowledge_id)
    if not row or row.user_id not in (KnowledgeModel.GLOBAL_USER_ID, uid):
        return None

    base = {"id": row.id, "title": row.title, "content": row.content}

    # 中文主导：原样返回
    if _looks_chinese((row.title or "") + "\n" + (row.content or "")[:800]):
        return {**base, "detected": "zh", "same_language": True, "cached": False}

    fingerprint = hashlib.md5(
        f"{row.title}\x00{row.content}".encode("utf-8")
    ).hexdigest()[:12]
    cache_key = CACHE_KEY.format(kid=row.id, fp=fingerprint)
    r = _redis()
    try:
        raw = r.get(cache_key)
        if raw:
            data = json.loads(raw)
            return {
                **base,
                "detected": data.get("detected") or "",
                "same_language": False,
                "title": data.get("title") or row.title,
                "content": data.get("content") or row.content,
                "cached": True,
            }
    except Exception:  # noqa: BLE001 —— 缓存读失败降级为重翻
        logger.warning("[translate] 缓存读取失败 id=%s", row.id, exc_info=True)

    content_out = translate_text(row.content or "")
    detected = content_out["detected"]
    if content_out["same_language"]:
        return {**base, "detected": detected or "zh", "same_language": True, "cached": False}
    title_out = (
        translate_text(row.title, from_lang=detected) if row.title else {"text": row.title}
    )
    result = {
        "detected": detected or "",
        "same_language": False,
        "title": title_out["text"] or row.title,
        "content": content_out["text"] or row.content,
    }
    try:
        r.set(
            cache_key,
            json.dumps(
                {"detected": result["detected"], "title": result["title"], "content": result["content"]},
                ensure_ascii=False,
            ),
            ex=CACHE_TTL_SEC,
        )
    except Exception:  # noqa: BLE001 —— 缓存写失败不阻塞返回
        logger.warning("[translate] 缓存写入失败 id=%s", row.id, exc_info=True)
    return {**base, **result, "cached": False}

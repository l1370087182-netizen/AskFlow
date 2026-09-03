"""入库质量门禁：判定一段爬取内容是否值得进知识库（宁缺毋滥）。

设计定位（供 producer 前置过滤 与 reviewer 严格质检共用，纯函数零副作用）：
- `rule_verdict`：规则层，只处理无争议垃圾（过短 / 导航壳 / 乱码），拿不准返回 None。
  producer 仅执行它的确信 discard 分支（不误杀拿不准的内容，那是 reviewer 的判定权）。
- `score_content`：LLM 给「知识价值」打 0-10 分，供 reviewer 按阈值定去留。
- 哲学：严格门禁——分数达标才留；但 LLM 失败绝不误删（删除不可逆）。
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# 入库判定统一的最小正文长度（producer.is_valid_article / reviewer / 门禁三处同口径）
MIN_CONTENT_LEN = 200

# 导航/模板残留标记词（命中种类多 + 正文短 → 判定抓到的是导航壳而非正文）
BOILERPLATE_MARKERS = (
    "下一页", "上一页", "导航", "索引", "版权", "copyright", "logo",
    "跳转到内容", "搜索", "菜单", "登录", "注册", "cookie",
)

# 质量门禁阈值（严格度旋钮）：LLM 知识价值分 ≥ 它才留。
# 调高前先跑 scripts/review_personal_kb.py --dry-run 采样，看分布再定线。
QUALITY_MIN_SCORE = 6.0

# score_content 送进 LLM 的正文截断长度（沿用现状，够用且省 token）
SCORE_CONTENT_LIMIT = 2000

# 乱码/编码异常判定：替换符/控制字符占比过高 → 抓取编码坏了，无知识价值
_GARBLED_RATIO_LIMIT = 0.20


# 0-10 打分提示词：分档锚点 + 用户点名的四类垃圾作负例，压低不同模型间的分值漂移
QUALITY_PROMPT = """你是技术知识库的质量守门员。请为下面这段爬取内容的「知识价值」打 0-10 分。

分档锚点：
- 9-10：优质完整的教程/文档，概念清晰、可直接学习
- 7-8：实质性干货，有技术要点/代码/原理，值得入库
- 5-6：平庸但可用，有一点有用信息，不完整或偏浅
- 3-4：噪声为主，信息量很低
- 0-2：纯垃圾，无知识价值

以下属于无价值内容，应打低分（≤4）：
- 导航页/目录索引/登录页/广告/404/模板文字堆砌
- 团队/合伙人/公司简介、联系我们、版权声明等站点样板
- 参考文献/引用列表/编辑历史/修订记录本身（而非正文）
- 与标题几乎无关的噪声

有实质技术内容（概念解释/原理/用法/代码示例/API 文档/教程）应打高分。

只输出 JSON：{{"score": 0到10的数字, "reason": "30字以内理由"}}

标题：{title}
分类：{category}
正文（前{limit}字）：
{content}"""


def rule_verdict(content: str) -> tuple[str, str] | None:
    """规则层：只返回确信的垃圾判定，拿不准返回 None 交 LLM。

    :return: ("discard", 原因) 或 None（不确定）
    """
    content = content or ""
    if len(content) < MIN_CONTENT_LEN:
        return "discard", "正文过短，无知识价值"
    # 乱码/编码异常占比过高
    sample = content[:3000]
    if sample:
        bad = sum(
            1 for ch in sample
            if ch == "�" or (ch.isspace() is False and ch.isprintable() is False)
        )
        if bad / len(sample) > _GARBLED_RATIO_LIMIT:
            return "discard", "乱码/编码异常，内容不可读"
    # 短正文 + 大量导航模板词 → 抓到的是导航壳而非正文
    head = content[:3000].lower()
    hits = sum(1 for m in BOILERPLATE_MARKERS if m.lower() in head)
    if len(content) < 400 and hits >= 3:
        return "discard", "正文以导航/模板文字为主"
    return None


def build_quality_prompt(title: str, category: str, content: str) -> str:
    return QUALITY_PROMPT.format(
        title=title or "（无标题）",
        category=category or "general",
        limit=SCORE_CONTENT_LIMIT,
        content=(content or "")[:SCORE_CONTENT_LIMIT],
    )


# 兼容多种模型输出："7"、"7/10"、"7.5分"、"score: 7"、JSON 里的 score
_SCORE_NUM = re.compile(r"(\d+(?:\.\d+)?)")


def parse_score(raw: str) -> float | None:
    """宽容提取 0-10 分数：先 JSON，再正则，最后 clamp；提不到返回 None"""
    t = (raw or "").strip()
    if not t:
        return None
    # 1) JSON
    stripped = re.sub(r"^```(?:json)?\s*", "", t)
    stripped = re.sub(r"\s*```$", "", stripped)
    candidates = [stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict) and data.get("score") is not None:
                return _clamp(float(data["score"]))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    # 2) 正则（兼容 "7/10"、"7.5分"、"score: 7"）
    m = _SCORE_NUM.search(t)
    if m:
        return _clamp(float(m.group(1)))
    return None


def _clamp(v: float) -> float:
    return max(0.0, min(10.0, v))


def score_content(llm, title: str, category: str, content: str, retries: int = 2) -> tuple[float | None, str]:
    """调 LLM 给内容打知识价值分（内置重试）。

    :return: (score, reason)；失败返回 (None, 原因)。调用方对 None 应保守保留。
    """
    prompt = build_quality_prompt(title, category, content)
    last_err = ""
    for attempt in range(1, max(1, retries) + 1):
        try:
            raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            score = parse_score(raw)
            if score is not None:
                reason = _extract_reason(raw)
                return score, reason or "模型评分"
            last_err = "模型输出无法解析出分数"
        except Exception as e:  # noqa: BLE001 —— 重试
            last_err = str(e)
            logger.warning("[quality] 评分失败（第 %s/%s 次）：%s", attempt, retries, e)
    return None, f"评分失败：{last_err}"[:200]


def _extract_reason(raw: str) -> str:
    """尽力从模型输出里取 reason 字段（取不到返回空串）"""
    t = (raw or "").strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", t)
    stripped = re.sub(r"\s*```$", "", stripped)
    candidates = [stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return str(data.get("reason", "")).strip()[:60]
        except json.JSONDecodeError:
            continue
    return ""

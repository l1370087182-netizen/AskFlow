"""检索编排器（同步，对话链路）：多查询改写 + 跨变体融合。

设计定位（见 docs §4.2A）：对话侧编排是请求内的同步函数组合，不 Agent 化——
任务认领只会加延迟、不带来并行。全链路降级：改写失败用原查询，
多路检索/重排任何一步异常或超预算都放行「原始单查询」结果，
对话永不因编排变慢变挂。

v1 编排动作：
1. 改写：用户问题 → ≤2 个变体查询（补术语/中英/拆解多意图）
2. 检索：原查询 + 变体 各跑一遍双路召回（BM25+向量）
3. 融合：所有路结果进同一个 RRF，复用现有融合与 Rerank
（结果评审/重检留给 v2——先拿到多查询融合的确定收益）
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from core.config import settings
from milvus.retrieval.hybird import rrf_fuse

logger = logging.getLogger(__name__)

ORCHESTRATE_BUDGET_SEC = 5.0     # 编排总预算：超预算的环节直接降级
REWRITE_WAIT_SEC = 8.0           # 改写调用最长等待（防慢模型拖垮对话）
MAX_VARIANTS = 2                 # 变体查询上限（不含原查询）

# 改写用独立小线程池：超时即弃（被弃线程由 httpx 超时自行了断）
_rewrite_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="orch-rewrite")

REWRITE_PROMPT = (
    "你是技术知识库的检索查询优化专家。把用户问题改写成最多 {max_variants} 个"
    "更容易命中文档的检索查询：\n"
    "- 补全术语（中英文、常见缩写）、拆解多意图问题为独立查询\n"
    "- 每条查询简洁（不超过 15 字），可直接用于检索，不要带问号以外的语气词\n"
    "- 原问题已经足够明确时，可以只输出 1 条与原问题等价或略优化的查询\n"
    "只输出 JSON：{{\"queries\": [\"...\"]}}"
)


def _extract_queries(raw: str) -> list[str]:
    """从模型回复宽容解析 queries 数组（剥围栏/截花括号），失败返回空"""
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
        except json.JSONDecodeError:
            continue
        qs = data.get("queries") if isinstance(data, dict) else None
        if isinstance(qs, list):
            out = [str(q).strip() for q in qs if str(q).strip()]
            return out[:MAX_VARIANTS]
    return []


class RetrievalOrchestrator:
    """对话链路的同步检索编排器：改写 + 多路召回 + 统一融合精排"""

    def __init__(self, retriever, llm):
        """
        :param retriever: HybridRetriever 实例（复用其 bm25/vector/reranker 与阈值）
        :param llm: 用于改写的对话模型（即当前对话使用的模型）
        """
        self.retriever = retriever
        self.llm = llm

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        uid: int = 0,
    ) -> list[dict]:
        """编排检索；任何异常都回退为普通单查询检索（不抛给对话层）"""
        try:
            variants = self._rewrite(query)
        except Exception as e:  # noqa: BLE001 —— 改写失败：原查询单飞
            logger.warning("[orchestrator] 改写失败，按原查询检索：%s", e)
            variants = []
        queries = [query] + [v for v in variants if v and v != query]
        queries = queries[: 1 + MAX_VARIANTS]
        if len(queries) == 1:
            return self.retriever.search(
                query, top_k=top_k, category=category, uid=uid
            )

        import time
        t0 = time.time()
        try:
            return self._multi_search(queries, top_k, category, uid, t0)
        except Exception as e:  # noqa: BLE001 —— 多路失败：回退单查询
            logger.warning("[orchestrator] 多查询检索失败，回退单查询：%s", e)
            return self.retriever.search(
                query, top_k=top_k, category=category, uid=uid
            )

    def _rewrite(self, query: str) -> list[str]:
        """LLM 改写（独立线程 + 等待上限，防慢模型拖垮对话）"""
        prompt = REWRITE_PROMPT.format(max_variants=MAX_VARIANTS) + f"\n\n用户问题：{query}"
        fut = _rewrite_pool.submit(self.llm.chat, [{"role": "user", "content": prompt}], 0.2)
        try:
            raw = fut.result(timeout=REWRITE_WAIT_SEC)
        except FutureTimeout:
            logger.warning("[orchestrator] 改写超时（>%ss），放弃改写", REWRITE_WAIT_SEC)
            return []
        return _extract_queries(raw)

    def _multi_search(
        self,
        queries: list[str],
        top_k: int,
        category: str | None,
        uid: int,
        t0: float,
    ) -> list[dict]:
        """每个变体跑双路召回 → 全部进同一 RRF 融合 → 一次 Rerank"""
        import time

        r = self.retriever
        lists: list[list[dict]] = []
        for q in queries:
            if time.time() - t0 > ORCHESTRATE_BUDGET_SEC:
                break  # 预算内能跑几路跑几路
            bm25_hits = r.bm25.search(
                q, top_k=r.bm25_top, category=category, uid=uid
            )
            try:
                vec_hits = r.vector.search(
                    q, top_k=r.vector_top, category=category, uid=uid
                )
            except Exception as e:  # noqa: BLE001 —— 与 HybridRetriever 同口径降级
                logger.warning("[orchestrator] 向量路不可用，该变体纯 BM25：%s", e)
                vec_hits = []
            lists.append(bm25_hits)
            lists.append(vec_hits)

        fused = rrf_fuse(lists)[: r.fuse_top]
        if not fused:
            return []

        # 预算不够精排 → 按 RRF 顺序放行
        if time.time() - t0 > ORCHESTRATE_BUDGET_SEC:
            logger.warning("[orchestrator] 预算耗尽，跳过 Rerank 按 RRF 顺序返回")
            return [{**doc, "score": doc["rrf_score"]} for doc in fused[:top_k]]

        try:
            reranked = r.reranker.rerank(
                queries[0], [d["content"] for d in fused], top_k=top_k
            )
        except Exception as e:  # noqa: BLE001 —— rerank 挂了按 RRF 顺序
            logger.warning("[orchestrator] Rerank 不可用，按 RRF 顺序返回：%s", e)
            return [{**doc, "score": doc["rrf_score"]} for doc in fused[:top_k]]

        results = []
        for rr in reranked:
            doc = fused[rr["index"]]
            results.append(
                {**doc, "score": rr["relevance_score"], "rerank_score": rr["relevance_score"]}
            )
        return results

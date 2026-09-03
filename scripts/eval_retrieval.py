"""检索质量评测：双路召回准确率 + RRF 融合 + Rerank 精排效果。

用法（项目根目录，需后端未占用且知识已向量化）：
    uv run python scripts/eval_retrieval.py [--sample N] [--quiet]

评测集三部分组成：
  1. 采样评测集：从已向量化全局知识里随机抽 N 篇，用「标题」当 query，
     同 knowledge_id 的块判为相关 —— ground truth 由库内数据自动导出，无需人工标注；
  2. 概念查询集：手写泛化问法（如「什么是依赖注入」），按关键词判相关；
     评测前先用 BM25 全语料预检关键词覆盖，库里根本没有的自动转负例口径；
  3. 负例集：知识领域外的问题（做菜/体育…），验证 rerank 阈值
     RELEVANCE_MIN_SCORE 判「知识库无资料」是否不误报。

度量口径：
  - 召回：BM25@20 / 向量@20 / 融合@30 各自能否捞到相关块（Recall）+ 最好名次
  - 双路互补：双路都命中 / 仅 BM25 / 仅向量 / 都没 —— 看融合相对单路的增益
  - 精排：RRF@5（未精排）vs Rerank@5 的 Hit@1 / MRR / NDCG@5 对比
  - 阈值：相关块与无关块的 rerank 分数分布、0.3 阈值的查准/查全
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台中文输出兜底
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import func  # noqa: E402

from core.config import settings  # noqa: E402,F401
from database.session import SessionLocal  # noqa: E402
from milvus.retrieval.bm25 import BM25Retriever  # noqa: E402
from milvus.retrieval.hybird import RELEVANCE_MIN_SCORE, rrf_fuse  # noqa: E402
from milvus.retrieval.reranker import Reranker  # noqa: E402
from milvus.retrieval.retriever import VectorRetriever  # noqa: E402
from model.KnowledgeModel import KnowledgeModel  # noqa: E402

# ---------- 手写概念查询（关键词判相关，任一命中即算相关） ----------
MANUAL_QUERIES: list[dict] = [
    {"q": "FastAPI 如何定义路径参数", "kw": ["路径参数"]},
    {"q": "什么是依赖注入", "kw": ["依赖注入"]},
    {"q": "FastAPI 中间件怎么写", "kw": ["中间件"]},
    {"q": "Python 列表推导式怎么用", "kw": ["列表推导"]},
    {"q": "Python 异常处理 try except", "kw": ["try:", "except", "异常处理"]},
    {"q": "Pydantic BaseModel 数据校验", "kw": ["BaseModel", "pydantic"]},
    {"q": "虚拟环境怎么创建和激活", "kw": ["虚拟环境", "venv"]},
]

# ---------- 负例：知识库领域外，期望 rerank 分全部低于阈值 ----------
NEGATIVE_QUERIES: list[str] = [
    "红烧肉的做法与火候控制",
    "量子纠缠的数学证明",
    "2026 世界杯赛程安排",
    "房贷等额本息计算公式",
]


# ---------- 指标工具 ----------

def first_rank(hits: list[dict], rel) -> int | None:
    """第一个相关块的名次（1 起），无则 None"""
    for i, h in enumerate(hits, start=1):
        if rel(h):
            return i
    return None


def hit_at(hits: list[dict], rel, k: int) -> int:
    return 1 if any(rel(h) for h in hits[:k]) else 0


def ndcg_at(hits: list[dict], rel, k: int) -> float:
    dcg = sum((1.0 / math.log2(i + 1)) for i, h in enumerate(hits[:k], start=1) if rel(h))
    n_rel = min(sum(1 for h in hits if rel(h)), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, n_rel + 1))
    return dcg / idcg if idcg else 0.0


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole * 100:.0f}%)" if whole else "0/0"


# ---------- 单 query 全链路 ----------

def run_query(query: str, rel, bm25, vector, reranker, fuse_top: int = 30):
    """跑一遍 双路召回 → RRF → Rerank（精排取全部候选分，供阈值分析）"""
    t0 = time.perf_counter()
    bm25_hits = bm25.search(query, top_k=20, uid=0)
    try:
        vec_hits = vector.search(query, top_k=20, uid=0)
        vec_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 向量路不可用：{e}")
        vec_hits, vec_ok = [], False

    fused = rrf_fuse([bm25_hits, vec_hits])[:fuse_top]
    rec = {
        "query": query,
        "bm25_rank": first_rank(bm25_hits, rel),
        "vec_rank": first_rank(vec_hits, rel) if vec_ok else None,
        "vec_ok": vec_ok,
        "bm25_hit20": hit_at(bm25_hits, rel, 20),
        "vec_hit20": hit_at(vec_hits, rel, 20) if vec_ok else 0,
        "fuse_rank": first_rank(fused, rel),
        "rrf5_hit1": hit_at(fused, rel, 1),
        "rrf5_mrr": (1.0 / r) if (r := first_rank(fused[:5], rel)) else 0.0,
        "rrf5_ndcg": ndcg_at(fused, rel, 5),
        "rerank_scores": [],   # [(score, is_rel), ...] 全部精排候选
        "final5_hit1": 0, "final5_mrr": 0.0, "final5_ndcg": 0.0,
        "rerank_lift": 0,      # 相关块名次提升（融合名次-精排名次）
        "elapsed_ms": 0,
    }

    if fused:
        rr = reranker.rerank(query, [d["content"] for d in fused], top_k=len(fused))
        ordered = [{**fused[r["index"]], "rerank_score": r["relevance_score"]} for r in rr]
        rec["rerank_scores"] = [(d["rerank_score"], bool(rel(d))) for d in ordered]
        rec["final5_hit1"] = hit_at(ordered, rel, 1)
        r5 = first_rank(ordered[:5], rel)
        rec["final5_mrr"] = (1.0 / r5) if r5 else 0.0
        rec["final5_ndcg"] = ndcg_at(ordered, rel, 5)
        if rec["fuse_rank"] and r5:
            rec["rerank_lift"] = rec["fuse_rank"] - r5

    rec["elapsed_ms"] = (time.perf_counter() - t0) * 1000
    return rec


# ---------- 主流程 ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="双路召回 + Rerank 评测")
    ap.add_argument("--sample", type=int, default=15, help="采样评测集大小（默认 15）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐 query 明细")
    args = ap.parse_args()

    db = SessionLocal()
    print("初始化 BM25 语料（首次可能重建缓存）…")
    bm25 = BM25Retriever(db)
    vector = VectorRetriever()
    reranker = Reranker()
    print(f"语料 {len(bm25.docs)} 块 | Milvus {vector.store.count()} 块 | 阈值 {RELEVANCE_MIN_SCORE}\n")
    if not bm25.docs:
        print("!! BM25 语料为空：没有 status=1 的知识，请先跑向量化（/api/embedding/run）")
        return

    records: list[dict] = []

    # ① 采样评测集：标题当 query，同文档块判相关
    rows = (
        db.query(KnowledgeModel.id, KnowledgeModel.title)
        .filter(
            KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED,
            (KnowledgeModel.user_id == 0) | (KnowledgeModel.user_id.is_(None)),
        )
        .all()
    )
    candidates = [(i, t) for i, t in rows if 4 <= len(t.strip()) <= 60 and t.strip() != "test"]
    random.Random(42).shuffle(candidates)
    sampled = candidates[: args.sample]
    print(f"═══ ① 采样评测（{len(sampled)} 篇，标题=query，同文档块=相关）═══")
    for kid, title in sampled:
        rec = run_query(title, lambda h, k=kid: h["knowledge_id"] == k, bm25, vector, reranker)
        rec["group"], rec["title"] = "sample", title
        records.append(rec)
        if not args.quiet:
            print(
                f"  [{kid}] {title[:34]:<36} bm25@{rec['bm25_rank'] or '-':>3} "
                f"vec@{rec['vec_rank'] or '-':>3} fuse@{rec['fuse_rank'] or '-':>3} "
                f"rerank@{(first_rank_from_rec(rec) or '-'):>2} {rec['elapsed_ms']:.0f}ms"
            )

    # ② 概念查询：先预检语料里有没有关键词，没有的按负例口径标注
    print(f"\n═══ ② 概念查询（{len(MANUAL_QUERIES)} 条，关键词判相关）═══")
    for mq in MANUAL_QUERIES:
        coverage = sum(
            1 for d in bm25.docs if any(k.lower() in d["content"].lower() for k in mq["kw"])
        )
        rel = (lambda h, kws=mq["kw"]: any(k.lower() in h["content"].lower() for k in kws))
        rec = run_query(mq["q"], rel, bm25, vector, reranker)
        rec["group"] = "manual"
        rec["coverage"] = coverage
        records.append(rec)
        if not args.quiet:
            tag = f"语料覆盖 {coverage} 块" if coverage else "!! 语料无覆盖(按负例看)"
            print(
                f"  {mq['q']:<28} {tag:<22} bm25@{rec['bm25_rank'] or '-':>3} "
                f"vec@{rec['vec_rank'] or '-':>3} fuse@{rec['fuse_rank'] or '-':>3} "
                f"rerank@{(first_rank_from_rec(rec) or '-'):>2}"
            )

    # ③ 负例：期望最高 rerank 分 < 阈值（判「无资料」正确）
    print(f"\n═══ ③ 负例（{len(NEGATIVE_QUERIES)} 条，期望判「无资料」）═══")
    for q in NEGATIVE_QUERIES:
        rec = run_query(q, lambda h: False, bm25, vector, reranker)
        rec["group"] = "negative"
        records.append(rec)
        top_score = max((s for s, _ in rec["rerank_scores"]), default=0.0)
        verdict = "✅ 正确判无资料" if top_score < RELEVANCE_MIN_SCORE else "❌ 误判有资料"
        if not args.quiet:
            print(f"  {q:<24} 最高 rerank 分 {top_score:.3f} → {verdict}")

    db.close()

    # ---------- 汇总 ----------
    samples = [r for r in records if r["group"] == "sample"]
    manuals = [r for r in records if r["group"] == "manual" and r.get("coverage", 0) > 0]
    positives = samples + manuals          # 有标准答案的正例
    negatives = [r for r in records if r["group"] == "negative"]

    print("\n" + "═" * 74)
    print("汇总（正例 = 采样 + 有语料覆盖的概念查询）")
    print("═" * 74)
    n = len(positives)
    if not n:
        print("没有可用正例，退出")
        return

    print(f"\n【双路召回率】正例 {n} 条，各阶段能否捞到相关块：")
    print(f"  BM25@20      召回 {pct(sum(r['bm25_hit20'] for r in positives), n)}")
    print(f"  向量@20      召回 {pct(sum(r['vec_hit20'] for r in positives), n)}")
    both = sum(1 for r in positives if r["bm25_hit20"] and r["vec_hit20"])
    only_b = sum(1 for r in positives if r["bm25_hit20"] and not r["vec_hit20"])
    only_v = sum(1 for r in positives if r["vec_hit20"] and not r["bm25_hit20"])
    neither = sum(1 for r in positives if not r["bm25_hit20"] and not r["vec_hit20"])
    print(f"  双路都命中 {both} | 仅 BM25 {only_b} | 仅向量 {only_v} | 都没 {neither}")
    fused_n = sum(1 for r in positives if r["fuse_rank"])
    rescued = sum(
        1 for r in positives
        if r["fuse_rank"] and not (r["bm25_hit20"] and r["vec_hit20"])
        and (r["bm25_hit20"] or r["vec_hit20"])
    )
    print(f"  RRF 融合@30  召回 {pct(fused_n, n)}（单路命中但名次靠后、被融合捞回的 {rescued} 条）")

    print("\n【精排效果】融合后 top5 对比（RRF 原序 vs Rerank 后）：")
    print(f"  {'指标':<12}{'RRF@5':>12}{'Rerank@5':>12}")
    print(f"  {'Hit@1':<12}{mean([r['rrf5_hit1'] for r in positives]):>12.2f}{mean([r['final5_hit1'] for r in positives]):>12.2f}")
    print(f"  {'MRR':<12}{mean([r['rrf5_mrr'] for r in positives]):>12.2f}{mean([r['final5_mrr'] for r in positives]):>12.2f}")
    print(f"  {'NDCG@5':<12}{mean([r['rrf5_ndcg'] for r in positives]):>12.2f}{mean([r['final5_ndcg'] for r in positives]):>12.2f}")
    lifted = [r["rerank_lift"] for r in positives if r["rerank_lift"]]
    if lifted:
        up = sum(1 for x in lifted if x > 0)
        print(f"  相关块名次变化：提升 {up} 条 / 下降 {sum(1 for x in lifted if x < 0)} 条，最大提升 {max(lifted)} 名")

    print("\n【阈值验证】rerank 分数分布与 0.3 阈值：")
    rel_scores = [s for r in positives for s, is_rel in r["rerank_scores"] if is_rel]
    irr_scores = [s for r in positives for s, is_rel in r["rerank_scores"] if not is_rel]
    if rel_scores:
        print(f"  相关块   {len(rel_scores)} 个：min {min(rel_scores):.3f} / 中位 {sorted(rel_scores)[len(rel_scores)//2]:.3f} / max {max(rel_scores):.3f}，≥{RELEVANCE_MIN_SCORE} 占 {pct(sum(1 for s in rel_scores if s >= RELEVANCE_MIN_SCORE), len(rel_scores))}")
    if irr_scores:
        fp = sum(1 for s in irr_scores if s >= RELEVANCE_MIN_SCORE)
        print(f"  无关块   {len(irr_scores)} 个：≥{RELEVANCE_MIN_SCORE}（误报）占 {pct(fp, len(irr_scores))}")
    neg_ok = 0
    for r in negatives:
        top_score = max((s for s, _ in r["rerank_scores"]), default=0.0)
        neg_ok += top_score < RELEVANCE_MIN_SCORE
    print(f"  负例判定 {pct(neg_ok, len(negatives))} 条正确判「无资料」")

    print(f"\n【性能】单 query 全链路平均 {mean([r['elapsed_ms'] for r in records]):.0f} ms（含 1 次 embedding + 1 次 rerank）")


def first_rank_from_rec(rec: dict) -> int | None:
    """精排后相关块名次（从 rerank_scores 推）"""
    for i, (_, is_rel) in enumerate(rec["rerank_scores"], start=1):
        if is_rel:
            return i
    return None


if __name__ == "__main__":
    main()

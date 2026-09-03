"""检索质量评测：双路召回准确率 + RRF 融合 + Rerank 精排效果。

用法（项目根目录，需后端未占用且知识已向量化）：
    uv run python scripts/eval_retrieval.py [--sample N] [--quiet]

评测集组成：
  1. 采样评测集：从已向量化全局知识里随机抽 N 篇，用「标题」当 query，
     同 knowledge_id 的块判为相关 —— ground truth 由库内数据自动导出，无需人工标注；
  2. 概念查询集：手写泛化问法（如「什么是依赖注入」），按关键词判相关；
  3. 负例集：知识领域外的问题（做菜/体育…），验证 rerank 阈值判「无资料」不误报；
  4. 泛化查询集（两组，逐条同义改写）：口语化问法与文档标题零词面重合，
     测真实检索力 + 换问法的稳定性；

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

# ---------- 泛化查询：口语化改写，问法与文档标题/关键词几乎不重合 ----------
# 这才是贴近真实用户的问法：BM25 没有表面词可匹配，主要考验向量路与融合的语义召回
HARD_QUERIES: list[dict] = [
    {"q": "URL 路径里那段值怎么传到处理函数里", "kw": ["路径参数"]},
    {"q": "前端提交的账号密码在后端怎么接收", "kw": ["表单"]},
    {"q": "多个接口共用的前置逻辑怎么只写一遍", "kw": ["依赖项", "Depends"]},
    {"q": "浏览器同源策略报错怎么解决", "kw": ["CORS", "跨域"]},
    {"q": "程序出错了怎么把现场信息记到文件里", "kw": ["logging", "日志"]},
    {"q": "Python 里支持异步的 HTTP 客户端库", "kw": ["httpx"]},
    {"q": "不启动真实服务怎么验证接口行为", "kw": ["TestClient"]},
    {"q": "服务器怎么把不断产生的数据实时推给网页", "kw": ["SSE", "服务器发送事件"]},
    {"q": "怎么让大模型去调外部提供的工具", "kw": ["Function Calling", "函数调用"]},
    {"q": "怎么量化一套检索问答系统做得好不好", "kw": ["评估"]},
    {"q": "接口入参的类型检查和自动报错用什么实现", "kw": ["pydantic", "校验"]},
    {"q": "一段话怎么变成能比较远近的数字表示", "kw": ["Embedding", "向量化"]},
]

# ---------- 泛化查询·第二组：与 HARD_QUERIES 逐条同义改写（一一对应） ----------
# 用途：① 扩充改写问法样本 ② 测换问法的稳定性——同一意图两种说法是否都能命中
HARD2_QUERIES: list[dict] = [
    {"q": "接口地址里的参数怎么在代码里拿到", "kw": ["路径参数"]},
    {"q": "用户填的登录信息后端用什么方式读", "kw": ["表单"]},
    {"q": "所有路由都要执行的公共检查写在哪里", "kw": ["依赖项", "Depends"]},
    {"q": "前端页面请求别的域名接口被拒怎么办", "kw": ["CORS", "跨域"]},
    {"q": "程序跑着跑着崩了怎么留痕排查", "kw": ["logging", "日志"]},
    {"q": "能发异步网络请求的 Python 包", "kw": ["httpx"]},
    {"q": "不部署上线怎么试接口写得对不对", "kw": ["TestClient"]},
    {"q": "后端有新消息怎么主动推给前端页面", "kw": ["SSE", "服务器发送事件"]},
    {"q": "大模型怎么自己决定去执行一个 API", "kw": ["Function Calling", "函数调用"]},
    {"q": "用什么指标衡量问答系统的回答质量", "kw": ["评估"]},
    {"q": "请求参数不合法时自动返回错误用什么库", "kw": ["pydantic", "校验"]},
    {"q": "两句话语义接不接近怎么让机器算", "kw": ["Embedding", "向量化"]},
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


# ---------- 关键词判相关的查询组（概念查询 / 泛化查询共用） ----------

def run_labeled_group(label, items, group, records, bm25, vector, reranker, quiet):
    print(f"\n═══ {label}（{len(items)} 条，关键词判相关）═══")
    for mq in items:
        coverage = sum(
            1 for d in bm25.docs if any(k.lower() in d["content"].lower() for k in mq["kw"])
        )
        rel = (lambda h, kws=mq["kw"]: any(k.lower() in h["content"].lower() for k in kws))
        rec = run_query(mq["q"], rel, bm25, vector, reranker)
        rec["group"] = group
        rec["coverage"] = coverage
        records.append(rec)
        if not quiet:
            tag = f"语料覆盖 {coverage} 块" if coverage else "!! 语料无覆盖(按负例看)"
            print(
                f"  {mq['q']:<30} {tag:<20} bm25@{rec['bm25_rank'] or '-':>3} "
                f"vec@{rec['vec_rank'] or '-':>3} fuse@{rec['fuse_rank'] or '-':>3} "
                f"rerank@{(first_rank_from_rec(rec) or '-'):>2}"
            )


# ---------- 主流程 ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="双路召回 + Rerank 评测")
    ap.add_argument("--sample", type=int, default=15, help="采样评测集大小（默认 15）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐 query 明细")
    ap.add_argument(
        "--only",
        default="",
        help="只跑指定组（逗号分隔）：sample,manual,negative,hard,hard2；默认全跑",
    )
    args = ap.parse_args()
    only = set(filter(None, args.only.split(",")))

    def want(group: str) -> bool:
        return not only or group in only

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
    sampled = candidates[: args.sample] if want("sample") else []
    if sampled:
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

    # ② 概念查询：问法与文档有表面词重合（偏易）；先预检语料里有没有关键词
    if want("manual"):
        run_labeled_group(
            "② 概念查询", MANUAL_QUERIES, "manual", records, bm25, vector, reranker, args.quiet
        )

    # ③ 负例：期望最高 rerank 分 < 阈值（判「无资料」正确）
    if want("negative"):
        print(f"\n═══ ③ 负例（{len(NEGATIVE_QUERIES)} 条，期望判「无资料」）═══")
        for q in NEGATIVE_QUERIES:
            rec = run_query(q, lambda h: False, bm25, vector, reranker)
            rec["group"] = "negative"
            records.append(rec)
            top_score = max((s for s, _ in rec["rerank_scores"]), default=0.0)
            verdict = "✅ 正确判无资料" if top_score < RELEVANCE_MIN_SCORE else "❌ 误判有资料"
            if not args.quiet:
                print(f"  {q:<24} 最高 rerank 分 {top_score:.3f} → {verdict}")

    # ④ 泛化查询：口语化改写，与文档几乎无表面词重合（真实用户问法）
    if want("hard"):
        run_labeled_group(
            "④ 泛化查询（改写问法）", HARD_QUERIES, "hard", records, bm25, vector, reranker, args.quiet
        )

    # ⑤ 泛化查询第二组：与④逐条同义改写，测换问法的稳定性
    if want("hard2"):
        run_labeled_group(
            "⑤ 泛化查询·第二组（同义改写）", HARD2_QUERIES, "hard2", records,
            bm25, vector, reranker, args.quiet,
        )

    db.close()

    # ---------- 汇总 ----------
    samples = [r for r in records if r["group"] == "sample"]
    manuals = [r for r in records if r["group"] == "manual" and r.get("coverage", 0) > 0]
    hards = [r for r in records if r["group"] == "hard" and r.get("coverage", 0) > 0]
    hards2 = [r for r in records if r["group"] == "hard2" and r.get("coverage", 0) > 0]
    positives = samples + manuals + hards + hards2   # 有标准答案的正例
    negatives = [r for r in records if r["group"] == "negative"]

    print("\n" + "═" * 74)
    print("汇总（正例 = 采样 + 有语料覆盖的概念查询）")
    print("═" * 74)
    n = len(positives)
    if not n:
        print("没有可用正例，退出")
        return

    print(f"\n【双路召回率】分组看各阶段能否捞到相关块：")
    print(f"  {'组别':<16}{'BM25@20':>14}{'向量@20':>14}{'融合@30':>14}")
    for name, grp in [
        ("采样·标题原题", samples),
        ("概念·词面重合", manuals),
        ("泛化·改写一", hards),
        ("泛化·改写二", hards2),
        ("合计", positives),
    ]:
        if not grp:
            continue
        g = len(grp)
        print(
            f"  {name:<16}"
            f"{pct(sum(r['bm25_hit20'] for r in grp), g):>14}"
            f"{pct(sum(r['vec_hit20'] for r in grp), g):>14}"
            f"{pct(sum(1 for r in grp if r['fuse_rank']), g):>14}"
        )
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

    # ④⑤ 逐条同义：同一意图两种问法是否都命中（检索稳定性）
    if hards and hards2 and len(hards) == len(hards2):
        pairs = list(zip(hards, hards2))
        both_ok = sum(1 for a, b in pairs if a["fuse_rank"] and b["fuse_rank"])
        unstable = [
            (a["query"], b["query"]) for a, b in pairs if bool(a["fuse_rank"]) != bool(b["fuse_rank"])
        ]
        print(f"\n【换问法稳定性】④/⑤ 同义问法两两对照 {len(pairs)} 对：")
        print(f"  两种问法都命中（融合@30）：{both_ok}/{len(pairs)}")
        for q1, q2 in unstable:
            print(f"  ⚠️ 同题不同命：「{q1}」 vs 「{q2}」")

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

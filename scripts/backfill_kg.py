"""存量知识补建图谱：给已入库但没进图谱的知识补抽实体+关系、建图。

为什么需要：图谱原本只在「爬取→质检」时搭车建（且只对有模型的用户内容）。
存量文档——尤其全局内容（user_id=0，服务端 v0.9 起无默认模型）——从没进过图，
多跳推理的图谱导航(B)因此无数据可走。本脚本【借用】一个已配置模型的用户
（或 .env CHAT_*）给存量内容建图；图谱归属仍按内容本身的 user_id（0=全局/>0=个人），
借谁的模型不影响归属，不会把全局内容算到某人名下。

与向量化的区别：本脚本只读 knowledge、只写 kg_node/kg_edge，【不碰 Milvus】，
所以可以在后端运行时直接跑（无 Milvus Lite 单进程独占问题）。

用法（项目根目录）：
    # 干跑：看会处理哪些（不调模型、不写图）
    uv run python scripts/backfill_kg.py --model-user 1 --scope all --dry-run
    # 实际建图（借用户 1 的模型）
    uv run python scripts/backfill_kg.py --model-user 1 --scope all --apply
    # 只补全局存量、限 20 篇
    uv run python scripts/backfill_kg.py --model-user 1 --scope global --limit 20 --apply
    # 用 .env CHAT_* 作模型（若配置了）
    uv run python scripts/backfill_kg.py --use-env-chat --scope global --apply

幂等：已建成图的 knowledge_id 记到 data/kg_backfill_done.json，重跑自动跳过，
不会把 mention_count / 边 weight 重复灌水。--rebuild 忽略该记录强制重建。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.config import settings  # noqa: E402
from database.session import SessionLocal  # noqa: E402
from model.KnowledgeModel import KnowledgeModel  # noqa: E402

DONE_FILE = Path(settings.DATA_DIR) / "kg_backfill_done.json"


def load_done() -> set[int]:
    """读已处理 id 集合；文件不存在/损坏视为空"""
    if not DONE_FILE.exists():
        return set()
    try:
        return {int(x) for x in json.loads(DONE_FILE.read_text(encoding="utf-8"))}
    except Exception:  # noqa: BLE001
        return set()


def save_done(done: set[int]) -> None:
    DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DONE_FILE.write_text(json.dumps(sorted(done)), encoding="utf-8")


def build_llm(db, args):
    """按来源构造模型：--model-user 借用户私有模型 / --use-env-chat 用 .env CHAT_*"""
    if args.model_user:
        from generation.llm import build_llm_for_user

        llm = build_llm_for_user(db, args.model_user)
        if llm is None:
            sys.exit(
                f"用户 {args.model_user} 未配置可用模型（base_url/api_key 为空）；"
                f"换一个 --model-user，或用 --use-env-chat"
            )
        return llm
    if args.use_env_chat:
        from generation.llm import ChatLLM

        try:
            return ChatLLM()  # 用 .env CHAT_KEY / CHAT_MODEL / CHAT_BASE_URL
        except Exception as e:  # noqa: BLE001
            sys.exit(f".env CHAT_* 未配置或不可用：{e}")
    sys.exit("需要指定模型来源：--model-user <id> 或 --use-env-chat")


def main() -> None:
    ap = argparse.ArgumentParser(description="存量知识补建图谱（借模型抽实体+关系建图）")
    ap.add_argument("--model-user", type=int, default=0, help="借用该用户已配置的私有模型")
    ap.add_argument("--use-env-chat", action="store_true", help="改用 .env CHAT_* 作模型")
    ap.add_argument("--scope", choices=["global", "personal", "all"], default="all",
                    help="补建范围：global=全局(user_id=0) / personal=个人(>0) / all=全部")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 篇（0=不限）")
    ap.add_argument("--apply", action="store_true", help="实际建图（默认只干跑）")
    ap.add_argument("--dry-run", action="store_true", help="只干跑（默认行为；与 --apply 同给时以干跑为准）")
    ap.add_argument("--rebuild", action="store_true", help="忽略 done 记录，强制重建（会重复灌 weight）")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run  # --dry-run 优先，防误建

    db = SessionLocal()
    done = set() if args.rebuild else load_done()

    # 选目标：按 scope 过滤 + 排除已处理 + 排序 + 限量
    q = db.query(KnowledgeModel)
    if args.scope == "global":
        q = q.filter(KnowledgeModel.user_id == 0)
    elif args.scope == "personal":
        q = q.filter(KnowledgeModel.user_id > 0)
    if done:
        q = q.filter(~KnowledgeModel.id.in_(done))
    q = q.order_by(KnowledgeModel.id)
    if args.limit > 0:
        q = q.limit(args.limit)
    rows = q.all()

    print(f"范围={args.scope} | 已处理记录={len(done)} 条 | 本次目标={len(rows)} 篇")
    if not rows:
        print("没有待补建的内容（都已处理或范围为空）。")
        db.close()
        return

    # 干跑：只报告，不调模型、不写图
    if not apply:
        print("\n[dry-run] 将处理以下条目（加 --apply 实际建图）：")
        for r in rows[:30]:
            owner = "全局" if r.user_id == 0 else f"用户{r.user_id}"
            print(f"  id={r.id:<5} [{owner}] [{r.source_type}] {(r.title or '')[:40]}")
        if len(rows) > 30:
            print(f"  … 其余 {len(rows) - 30} 篇省略")
        print("\n提示：--apply 需要可用模型（--model-user <id> 或 --use-env-chat）。")
        db.close()
        return

    llm = build_llm(db, args)
    from agents.quality import extract_graph
    from DAO.kg_dao import KgDAO

    built = empty = failed = 0
    nodes_total = 0
    for idx, row in enumerate(rows, 1):
        owner = "全局" if row.user_id == 0 else f"用户{row.user_id}"
        try:
            entities, triples = extract_graph(llm, row.title, row.category, row.content)
            if not (entities or triples):
                empty += 1
                print(f"  [{idx}/{len(rows)}] id={row.id} [{owner}] 无实体/关系产出，跳过")
                continue
            # 归属按内容本身 user_id（借谁的模型都不串号）
            n = KgDAO(db).learn_document(row.user_id, entities, row.category, triples)
            nodes_total += n
            built += 1
            done.add(row.id)
            print(f"  [{idx}/{len(rows)}] id={row.id} [{owner}] 《{(row.title or '')[:30]}》"
                  f"→ {n} 节点 / {len(triples)} 关系")
            if idx % 10 == 0:
                save_done(done)  # 周期性落盘，中途失败不丢进度
        except Exception as e:  # noqa: BLE001 —— 单篇失败不中断整轮
            failed += 1
            print(f"  [{idx}/{len(rows)}] id={row.id} [{owner}] 建图失败：{repr(e)[:100]}")

    save_done(done)
    print(f"\n完成：建成 {built} 篇（{nodes_total} 节点）| 无产出 {empty} 篇 | 失败 {failed} 篇")
    print(f"已处理记录写入 {DONE_FILE}（重跑自动跳过；--rebuild 强制重建）")
    db.close()


if __name__ == "__main__":
    main()

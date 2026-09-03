"""个人知识库质量补审：用新的严格门禁复检存量爬取内容，清理低质量条目。

背景：质量门禁上线前爬取的内容没经过严格质检，可能混入垃圾。本脚本按新标准
复检，把知识价值分低于阈值（QUALITY_MIN_SCORE）的条目交给 reviewer 删除。

安全设计（删除不可逆，层层保险）：
- 默认 --dry-run：只采样评分、打印分布，不动任何数据（先校准阈值！）
- --apply 才真正建质检任务；且只建任务，审核仍由 reviewer 执行（脚本不亲自删）
- 只碰 user_id>0 的个人爬取内容；硬编码排除全局语料（user_id=0）与手写笔记（manual://）
- --max-rows 熔断，防止一次误删过多

用法：
    # 第 1 步：采样校准（强烈建议先看分布，再决定阈值是否合适）
    uv run python scripts/review_personal_kb.py --user 4
    # 第 2 步：确认无误后真正执行（建质检任务，需后端在跑）
    uv run python scripts/review_personal_kb.py --user 4 --apply
    # 其他：--batch 50 分片大小 / --max-rows 500 熔断 / --all 连已评过的也重评
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows GBK 控制台打不了 emoji/生僻字符，降级为占位而非崩溃
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

from DAO.agent_task_dao import AgentTaskDAO  # noqa: E402
from agents.quality import QUALITY_MIN_SCORE, score_content  # noqa: E402
from database.session import SessionLocal  # noqa: E402
from generation.llm import build_llm_for_user  # noqa: E402
from model.AgentTaskModel import TaskKind  # noqa: E402
from model.KnowledgeModel import KnowledgeModel  # noqa: E402

DRY_RUN_SAMPLE = 30   # dry-run 采样条数


def _target_query(db, user_id: int, include_scored: bool):
    """待补审集合：本人个人知识、排除手写笔记；默认只挑未评的（quality_score IS NULL）"""
    q = db.query(KnowledgeModel).filter(
        KnowledgeModel.user_id == user_id,
        ~KnowledgeModel.source_url.like("manual://%"),
    )
    if not include_scored:
        q = q.filter(KnowledgeModel.quality_score.is_(None))
    return q.order_by(KnowledgeModel.id.asc())


def dry_run(db, llm, user_id: int) -> None:
    """采样评分、打印分布，供校准阈值（不动数据）"""
    rows = _target_query(db, user_id, include_scored=False).limit(DRY_RUN_SAMPLE).all()
    if not rows:
        print("没有待补审的内容（都已评分或无个人爬取内容）。")
        return
    print(f"采样 {len(rows)} 条（未评分的个人爬取内容）逐条评分，阈值 {QUALITY_MIN_SCORE}：\n")
    scores = []
    below = 0
    for row in rows:
        score, reason = score_content(llm, row.title, row.category, row.content)
        if score is None:
            print(f"  [评分失败] id={row.id} 《{row.title[:30]}》 {reason}")
            continue
        mark = "✓留" if score >= QUALITY_MIN_SCORE else "✗删"
        print(f"  [{score:.0f} {mark}] id={row.id} 《{row.title[:30]}》 {reason[:40]}")
        scores.append(score)
        if score < QUALITY_MIN_SCORE:
            below += 1
    if not scores:
        print("\n全部评分失败（检查用户模型配置），无法校准。")
        return
    avg = sum(scores) / len(scores)
    print(f"\n成功评分 {len(scores)} 条：平均 {avg:.1f} 分，"
          f"低于阈值 {QUALITY_MIN_SCORE} 的 {below} 条（预计丢弃 {below/len(scores)*100:.0f}%）。")
    if below / len(scores) > 0.5:
        print("⚠️ 预计丢弃超过一半，确认阈值/内容质量后再 --apply。")
    else:
        print("如分布合理，用 --apply 执行清理。")


def apply(db, user_id: int, batch: int, max_rows: int, include_scored: bool) -> None:
    """分片建质检任务（真正删除由 reviewer 执行；需后端在跑）"""
    rows = _target_query(db, user_id, include_scored).limit(max_rows).all()
    if not rows:
        print("没有可补审的内容。")
        return
    ids = [r.id for r in rows]
    if len(ids) >= max_rows:
        print(f"⚠️ 达到 --max-rows 熔断上限 {max_rows}，本次只处理前 {max_rows} 条，可多次运行。")

    dao = AgentTaskDAO(db)
    batch = max(1, min(batch, 50))  # reviewer 单任务上限 50，防静默截断
    tasks = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        dao.create(
            kind=TaskKind.QUALITY_REVIEW,
            user_id=user_id,
            payload={"knowledge_ids": chunk, "backfill": True},
            agent="backfill-script",
        )
        tasks += 1
    print(f"已建 {tasks} 个存量补审质检任务（共 {len(ids)} 条），"
          f"后端运行时 reviewer 会逐条严格复检并删除低质内容。")


def main() -> None:
    parser = argparse.ArgumentParser(description="个人知识库质量补审")
    parser.add_argument("--user", type=int, required=True, help="用户 id（必须 >0，不含全局）")
    parser.add_argument("--apply", action="store_true", help="真正建质检任务（默认只 dry-run 采样）")
    parser.add_argument("--batch", type=int, default=50, help="每个质检任务的条数（≤50）")
    parser.add_argument("--max-rows", type=int, default=500, help="本次最多处理的行数（熔断）")
    parser.add_argument("--all", action="store_true", help="连已评过分的也重评（默认只评未评的）")
    args = parser.parse_args()

    if args.user <= 0:
        print("--user 必须 >0（全局语料 user_id=0 不在补审范围）。")
        sys.exit(1)

    db = SessionLocal()
    try:
        if args.apply:
            apply(db, args.user, args.batch, args.max_rows, args.all)
        else:
            llm = build_llm_for_user(db, args.user)
            if llm is None:
                print("该用户未配置个人模型，无法评分。请先到「对话学习」页 ⚙️ 配置模型。")
                sys.exit(1)
            dry_run(db, llm, args.user)
    finally:
        db.close()


if __name__ == "__main__":
    main()

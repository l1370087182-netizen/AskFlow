"""术语种子数据生成：从已入库文章标题批量提炼术语，灌进 tech_term 表。

对应 CLAUDE.md §5.2「初期从已入库文章标题 + LLM 辅助提炼」。
每批把若干 (分类, 标题) 喂给 ChatLLM，让它输出结构化术语列表，
去重后写入数据库（create_if_absent 幂等，重复执行安全）。

用法：
    uv run python scripts/seed_terms.py            # 默认每批 40 条标题
    uv run python scripts/seed_terms.py -b 20      # 自定义批大小
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from DAO.tech_term_dao import TechTermDAO  # noqa: E402
from database.session import SessionLocal  # noqa: E402
from generation.llm import ChatLLM  # noqa: E402
from model.KnowledgeModel import KnowledgeModel  # noqa: E402

SEED_PROMPT = """下面是一份编号的技术文章标题清单：

{listing}

请从中提炼出值得做成「每日学习卡片」的技术术语，要求：
1. 只输出一个 JSON 数组，不要输出任何其他文字，不要加 markdown 代码块；
2. 每个元素格式：
   {{"index": 来源标题的编号, "term": 术语（简短名词，优先通用英文写法）, "alias": 别名（逗号分隔，没有就写空字符串）, "brief": 一句话中文简介（不超过 50 字）, "detail": 详细讲解（2-4 句，用通俗语言讲清原理和适用场景）, "example": 一个简短示例（代码或应用场景，代码用纯文本不要加 ``` 包裹）}}
3. 只提取真正的技术概念/工具/方法，忽略「主页」「变更日志」「社区介绍」「翻译说明」这类非技术标题；
4. 同一个术语只保留一条；
5. index 必须是上面清单里出现过的编号。
"""

ENRICH_PROMPT = """下面是一份编号的技术术语清单（含一句话简介）：

{listing}

请为每个术语补充「详细讲解」和「示例」，只输出一个 JSON 数组，不要输出其他文字：
[{{"index": 编号, "detail": 详细讲解（2-4 句，通俗讲清原理和适用场景）, "example": 一个简短示例（代码或应用场景，代码用纯文本不要加 ``` 包裹）}}]
"""


def build_listing(batch: list[KnowledgeModel], start_no: int) -> str:
    """拼出编号标题清单：[编号] (分类) 标题"""
    lines = []
    for i, row in enumerate(batch, start=start_no):
        lines.append(f"[{i}] ({row.category}) {row.title}")
    return "\n".join(lines)


def parse_terms(raw: str) -> list[dict]:
    """从模型回复里解析 JSON 数组，容忍代码块包裹"""
    text = raw.strip()
    # 去掉可能的 ```json ... ``` 包裹
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    # 截取第一个 [ 到最后一个 ] 之间的内容，防模型多说废话
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
        return [d for d in data if isinstance(d, dict)]
    except json.JSONDecodeError:
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 辅助提炼术语种子数据")
    parser.add_argument("-b", "--batch", type=int, default=40, help="每批标题数")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="只补全存量术语的详细讲解与示例（不新增术语）",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        llm = ChatLLM()
        dao = TechTermDAO(db)
        if args.enrich:
            enrich(db, dao, llm, args.batch)
            return

        rows = (
            db.query(KnowledgeModel)
            .filter(KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED)
            .order_by(KnowledgeModel.id)
            .all()
        )
        if not rows:
            print("没有已向量化的知识，先跑流水线再灌术语")
            return

        # 标题编号 → 行，用于回填 source_url 与分类
        by_no: dict[int, KnowledgeModel] = {}
        for i, row in enumerate(rows, start=1):
            by_no[i] = row

        total_new = 0
        for start in range(1, len(rows) + 1, args.batch):
            batch = rows[start - 1 : start - 1 + args.batch]
            listing = build_listing(batch, start)
            prompt = SEED_PROMPT.format(listing=listing)

            try:
                raw = llm.chat(
                    [{"role": "user", "content": prompt}], temperature=0.2
                )
            except Exception as e:  # noqa: BLE001 —— 单批失败不中断整体
                print(f"[{start}~{start + len(batch) - 1}] LLM 调用失败：{e}")
                continue

            terms = parse_terms(raw)
            added = 0
            for t in terms:
                idx = t.get("index")
                term = str(t.get("term", "")).strip()
                src = by_no.get(idx) if isinstance(idx, int) else None
                if not term or src is None:
                    continue  # index 越界/缺失的条目丢弃
                row = dao.create_if_absent(
                    term=term,
                    alias=str(t.get("alias", "")).strip(),
                    category=src.category,
                    brief=str(t.get("brief", "")).strip(),
                    source_url=src.source_url,
                    detail=str(t.get("detail", "")).strip(),
                    example=str(t.get("example", "")).strip(),
                )
                if row:
                    added += 1
            total_new += added
            print(f"[{start}~{start + len(batch) - 1}] 提炼 {len(terms)} 条，新增 {added} 条")

        print(f"完成：共新增术语 {total_new} 条，当前总数 {dao.count()}")
    finally:
        db.close()


def enrich(db, dao: TechTermDAO, llm: ChatLLM, batch: int) -> None:
    """为缺少详细讲解的存量术语批量补 detail/example"""
    missing = dao.list_missing_detail()
    if not missing:
        print("没有需要补全的术语")
        return
    print(f"待补全 {len(missing)} 条")

    done = 0
    for start in range(0, len(missing), batch):
        chunk = missing[start : start + batch]
        listing = "\n".join(
            f"[{i}] ({t.category}) {t.term} —— {t.brief}"
            for i, t in enumerate(chunk, start=1)
        )
        try:
            raw = llm.chat(
                [{"role": "user", "content": ENRICH_PROMPT.format(listing=listing)}],
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{start}~{start + len(chunk) - 1}] LLM 调用失败：{e}")
            continue

        items = parse_terms(raw)
        updated = 0
        for item in items:
            idx = item.get("index")
            if not isinstance(idx, int) or not (1 <= idx <= len(chunk)):
                continue
            detail = str(item.get("detail", "")).strip()
            example = str(item.get("example", "")).strip()
            if not detail:
                continue
            dao.update_enrichment(chunk[idx - 1].id, detail, example)
            updated += 1
        done += updated
        print(f"[{start + 1}~{start + len(chunk)}] 补全 {updated} 条")

    print(f"补全完成：共 {done} 条")


if __name__ == "__main__":
    main()

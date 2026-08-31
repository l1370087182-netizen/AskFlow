"""
初始化数据库表。
用法（在项目根目录执行）：
    uv run python scripts/init_db.py
"""
import sys
from pathlib import Path

# 把 src/ 加进模块搜索路径，才能 import core / database / model
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# 必须先 import Model，Base.metadata 里才会有这些表
import model.KnowledgeModel  # noqa: F401
import model.TechTermModel  # noqa: F401
import model.JDModel  # noqa: F401
import model.TechStackModel  # noqa: F401
import model.EvaluateModel  # noqa: F401

from database.session import init_db, engine
from sqlalchemy import text


def main() -> None:
    print("正在连接数据库并建表...")
    init_db()
    print("建表完成。")

    # 抽查：表是否都存在
    with engine.connect() as conn:
        for table in ("knowledge", "tech_term", "jd", "tech_stack", "evaluate"):
            rows = conn.execute(text(f"SHOW TABLES LIKE '{table}'")).fetchall()
            if rows:
                print(f"OK: 表 {table} 已存在")
            else:
                print(f"FAIL: 未找到表 {table}")


if __name__ == "__main__":
    main()
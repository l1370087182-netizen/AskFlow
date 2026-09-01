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
import model.UserModel  # noqa: F401

from database.session import init_db, engine
from sqlalchemy import text


def ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """给已存在的表补列（create_all 不会给已有表加列）。

    用于阶段 11：evaluate / jd 两表补 user_id（存量行留 NULL，旧数据作废）。
    """
    exists = conn.execute(
        text(
            "SELECT 1 FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).first()
    if exists:
        print(f"OK: {table}.{column} 已存在，跳过")
        return
    conn.execute(
        text(
            f"ALTER TABLE `{table}` ADD COLUMN {ddl}, "
            f"ADD INDEX `ix_{table}_{column}` (`{column}`)"
        )
    )
    conn.commit()
    print(f"OK: 已为表 {table} 补列 {column}")


def main() -> None:
    print("正在连接数据库并建表...")
    init_db()
    print("建表完成。")

    # 存量表补 user_id 列（幂等）
    with engine.connect() as conn:
        ensure_column(conn, "evaluate", "user_id", "user_id INT NULL COMMENT '所属用户'")
        ensure_column(conn, "jd", "user_id", "user_id INT NULL COMMENT '所属用户'")

    # 抽查：表是否都存在
    with engine.connect() as conn:
        for table in ("knowledge", "tech_term", "jd", "tech_stack", "evaluate", "user"):
            rows = conn.execute(text(f"SHOW TABLES LIKE '{table}'")).fetchall()
            if rows:
                print(f"OK: 表 {table} 已存在")
            else:
                print(f"FAIL: 未找到表 {table}")


if __name__ == "__main__":
    main()
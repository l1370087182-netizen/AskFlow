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
import model.AgentTaskModel  # noqa: F401

from database.session import init_db, engine
from sqlalchemy import text


def ensure_column(conn, table: str, column: str, ddl: str) -> bool:
    """给已存在的表补列（create_all 不会给已有表加列），返回本次是否真补了列。

    - 阶段 11：evaluate / jd 两表补 user_id（存量行留 NULL，旧数据作废）。
    - 个人知识库：knowledge 补 user_id NOT NULL DEFAULT 0（存量行自动落 0=全局）。
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
        return False
    conn.execute(
        text(
            f"ALTER TABLE `{table}` ADD COLUMN {ddl}, "
            f"ADD INDEX `ix_{table}_{column}` (`{column}`)"
        )
    )
    conn.commit()
    print(f"OK: 已为表 {table} 补列 {column}")
    return True


def ensure_knowledge_unique(conn) -> None:
    """knowledge 唯一约束迁移：单列 source_url → 复合 (user_id, source_url(700))。

    复合唯一让 source_url 保持真实值：详情「原文链接」直接渲染、
    get_by_url(url, user_id) 语义干净、不同用户收藏同 URL 天然共存。

    坑：source_url 是 varchar(768)，utf8mb4 下 ×4 = 3072 字节已顶到 InnoDB
    索引上限，复合索引必须用前缀 source_url(700)；DAO 层保留全值判重兜底。
    按存在性裁剪子句，单条 ALTER 完成，幂等可重跑。
    """
    rows = conn.execute(
        text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'knowledge'"
        )
    ).fetchall()
    indexes = {r[0] for r in rows}

    clauses = []
    if "uq_knowledge_source_url" in indexes:
        clauses.append("DROP INDEX `uq_knowledge_source_url`")
    if "uq_knowledge_user_url" not in indexes:
        clauses.append(
            "ADD UNIQUE KEY `uq_knowledge_user_url` (`user_id`, `source_url`(700))"
        )
    if not clauses:
        print("OK: knowledge 已是复合唯一索引 (user_id, source_url)，跳过")
        return

    conn.execute(text(f"ALTER TABLE `knowledge` {', '.join(clauses)}"))
    conn.commit()
    print(f"OK: knowledge 唯一索引已迁移：{', '.join(clauses)}")

    # 双确认：SHOW INDEX 复核，避免静默失败
    after = conn.execute(text("SHOW INDEX FROM `knowledge`")).fetchall()
    names = {r[2] for r in after}  # Key_name 在第 3 列
    assert "uq_knowledge_user_url" in names, "复合唯一索引未生效，请人工检查"
    assert "uq_knowledge_source_url" not in names, "旧唯一索引未删除，请人工检查"
    print("OK: SHOW INDEX 复核通过（uq_knowledge_user_url 已就位）")


def main() -> None:
    print("正在连接数据库并建表...")
    init_db()
    print("建表完成。")

    # 存量表补 user_id 列（幂等）
    with engine.connect() as conn:
        ensure_column(conn, "evaluate", "user_id", "user_id INT NULL COMMENT '所属用户'")
        ensure_column(conn, "jd", "user_id", "user_id INT NULL COMMENT '所属用户'")
        # 管理员后台展示「最近登录时间」（存量用户为 NULL = 功能上线前注册）
        ensure_column(
            conn, "user", "last_login_at",
            "last_login_at DATETIME NULL COMMENT '最近登录时间'",
        )

    # 个人知识库迁移（幂等）：
    # 1) knowledge 补 user_id（NOT NULL DEFAULT 0，存量行自动落 0=全局）
    # 2) 唯一约束换复合索引 (user_id, source_url(700))
    # 3) 仅当本次真补了列 → 全部知识重置为待向量化：
    #    Milvus 集合将 drop 重建（新 schema 带 user_id 字段），
    #    需要重跑 /api/embedding/run 回灌；绑在补列成功分支，重跑不反复清零
    with engine.connect() as conn:
        added = ensure_column(
            conn,
            "knowledge",
            "user_id",
            "user_id INT NOT NULL DEFAULT 0 COMMENT '所属用户；0=全局知识'",
        )
        ensure_knowledge_unique(conn)
        if added:
            conn.execute(text("UPDATE `knowledge` SET `status` = 0"))
            conn.commit()
            print("OK: knowledge.status 已全量重置为 0（待向量化），"
                  "启动后端后请重跑 /api/embedding/run 回灌向量库")

    # 抽查：表是否都存在
    with engine.connect() as conn:
        for table in ("knowledge", "tech_term", "jd", "tech_stack", "evaluate", "user", "agent_task"):
            rows = conn.execute(text(f"SHOW TABLES LIKE '{table}'")).fetchall()
            if rows:
                print(f"OK: 表 {table} 已存在")
            else:
                print(f"FAIL: 未找到表 {table}")


if __name__ == "__main__":
    main()
"""知识库扩充：LLM 生成「AI 应用开发高频问题 + 重要 Python 库」讲解文档并入库。

对应需求：知识库需要 AI 应用开发常问问题讲解、重要 python 库（Pydantic/PyCharm 等）。
每篇生成 markdown 讲解（是什么/为什么/怎么用/代码示例/面试常问），
走 KnowledgeDAO.upsert 入库（source_type=generated），之后跑向量化流水线即可被检索。

用法：
    uv run python scripts/seed_ai_knowledge.py          # 全量生成（已存在的跳过）
    uv run python scripts/seed_ai_knowledge.py --ai     # 只生成 AI 类
    uv run python scripts/seed_ai_knowledge.py --py     # 只生成 Python 类
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from DAO.knowledge_dao import KnowledgeDAO  # noqa: E402
from database.session import SessionLocal  # noqa: E402
from generation.llm import ChatLLM  # noqa: E402

# (分类, slug, 标题, 讲解侧重点)
AI_TOPICS = [
    ("ai", "rag", "什么是 RAG（检索增强生成）", "定义、检索-增强-生成的完整流程、为什么能缓解幻觉、适用场景"),
    ("ai", "embedding", "什么是 Embedding（文本向量化）", "把文本映射成高维向量的原理、余弦相似度、bge-m3 这类模型的特点"),
    ("ai", "vector-db", "向量数据库选型：Milvus、FAISS、Chroma", "三者定位差异、索引类型（HNSW/IVF）、选型建议"),
    ("ai", "chunking", "RAG 切块（Chunking）策略", "块大小与重叠的权衡、按段落/标题切分、递归切块、块太大会怎样"),
    ("ai", "hybrid-retrieval", "混合检索：BM25 + 向量 + RRF 融合", "关键词与语义检索互补、RRF 公式与为什么不用加权求和"),
    ("ai", "rerank", "什么是 Rerank（重排序）", "双塔召回后交叉编码器精排的原理、bge-reranker 用法、top-k 取舍"),
    ("ai", "prompt-engineering", "提示词工程（Prompt Engineering）", "角色设定、结构化指令、少样本示例、系统提示与用户提示分工"),
    ("ai", "hallucination", "大模型幻觉：成因与缓解手段", "幻觉的定义与成因、检索接地、引用来源、温度调低、承认不知道"),
    ("ai", "agent", "什么是 AI Agent（智能体）", "感知-规划-行动-记忆循环、工具调用、与 RAG 的区别与结合"),
    ("ai", "function-calling", "什么是 Function Calling（函数调用）", "模型输出结构化工具调用、schema 定义、执行回传、多轮工具循环"),
    ("ai", "finetune-vs-rag", "微调（Fine-tuning）与 RAG 怎么选", "知识注入 vs 行为塑造、成本与时效对比、两者结合的场合"),
    ("ai", "token-context", "Token 与上下文窗口", "分词原理、上下文窗口限制、超长文本的处理策略、token 与费用"),
    ("ai", "sse-streaming", "大模型流式输出与 SSE", "为什么用流式、SSE 事件格式、与 WebSocket 的取舍、前端逐字渲染"),
    ("ai", "rag-evaluation", "如何评估 RAG 系统效果", "检索侧（召回率/MRR）与生成侧（忠实度/答案相关性）、人工评测清单"),
]

PY_TOPICS = [
    ("python", "pydantic", "Pydantic：数据校验与类型转换库", "BaseModel、字段约束 Field、自动类型转换、与 FastAPI 的集成原理"),
    ("python", "numpy", "NumPy：数组与数值计算", "ndarray、向量化运算为什么快、广播机制、常见操作示例"),
    ("python", "pandas", "Pandas：数据分析利器", "DataFrame/Series、读取 csv、筛选分组、与 NumPy 的关系"),
    ("python", "httpx-requests", "Requests 与 HTTPX：HTTP 客户端对比", "requests 的简单够用、httpx 的异步与 HTTP/2、超时与重试写法"),
    ("python", "sqlalchemy", "SQLAlchemy：Python ORM 框架", "Model 定义、Session 会话、query 查询、engine 与连接池"),
    ("python", "pytest", "Pytest 测试框架", "测试函数与断言、fixture、参数化 parametrize、常用插件"),
    ("python", "pycharm", "PyCharm IDE 高效使用", "项目解释器配置、调试器断点、重构与跳转、常用快捷键"),
    ("python", "uv-poetry", "uv 与 Poetry：新一代依赖管理", "uv 为什么快、虚拟环境与锁文件、与 pip 的对比、常用命令"),
    ("python", "redis-py", "Redis 与 redis-py：缓存与队列", "常用数据结构、set/get、list 做队列、set 做去重、过期时间"),
    ("python", "logging", "Python 日志系统 logging", "logger/level/handler、为什么别用 print、格式化与文件输出"),
]

ARTICLE_PROMPT = """你是一位资深 AI 应用开发工程师兼讲师。请围绕「{title}」写一篇技术讲解文档，它将入库 RAG 知识库供检索学习。

要求：
1. 用 markdown 格式，结构为：## 是什么 / ## 为什么重要 / ## 核心用法 / ## 代码示例 / ## 面试常问；
2. 通俗易懂，先大白话后术语；代码示例用纯 Python，放在 ```python 代码块里；
3. 「面试常问」部分列 3-5 条问答，每条一两行；
4. 全文 800-1500 字，不要废话，不要输出文档标题以外的任何解释性文字。

讲解侧重点：{focus}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 AI/Python 知识文档入库")
    parser.add_argument("--ai", action="store_true", help="只生成 AI 类")
    parser.add_argument("--py", action="store_true", help="只生成 Python 类")
    args = parser.parse_args()

    topics = []
    if args.ai or not (args.ai or args.py):
        topics += AI_TOPICS
    if args.py or not (args.ai or args.py):
        topics += PY_TOPICS

    db = SessionLocal()
    try:
        dao = KnowledgeDAO(db)
        llm = ChatLLM()
        added = skipped = failed = 0

        for category, slug, title, focus in topics:
            source_url = f"generate://{slug}"
            if dao.get_by_url(source_url):
                skipped += 1
                print(f"跳过（已存在）: {title}")
                continue
            try:
                content = llm.chat(
                    [{"role": "user", "content": ARTICLE_PROMPT.format(title=title, focus=focus)}],
                    temperature=0.4,
                ).strip()
            except Exception as e:  # noqa: BLE001 —— 单篇失败不中断
                failed += 1
                print(f"失败: {title} -> {e}")
                time.sleep(2)
                continue
            if len(content) < 300:
                failed += 1
                print(f"失败（内容过短）: {title}")
                continue
            dao.upsert(
                title=title,
                content=content,
                source_url=source_url,
                category=category,
                source_type="generated",
            )
            added += 1
            print(f"入库: [{category}] {title}（{len(content)} 字）")

        print(f"完成：新增 {added}，跳过 {skipped}，失败 {failed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

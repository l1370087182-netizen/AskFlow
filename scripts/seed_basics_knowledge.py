"""知识库重置回填：LLM 生成「Python / Java / C/C++ 基础」三类讲解文档入库。

背景：全局知识库清空重置（2026-09-03），按需求换成三门语言的基础知识。
每篇生成 markdown 讲解（是什么/为什么/核心用法/代码示例/面试常问），
走 KnowledgeDAO.upsert 入库（source_type=generated，全局），之后跑向量化流水线即可被检索。

模型来源（v0.9 起服务端无默认模型，二选一）：
    1) 命令行直接传临时模型：--base-url xxx --api-key xxx --model xxx
    2) .env 的 CHAT_* 配置（缺省时回退）

用法：
    uv run python scripts/seed_basics_knowledge.py --base-url ... --api-key ... --model ...
    可加 --python / --java / --cpp 只生成某一类（缺省三类全生成）
    --dry-run 只列主题不入库
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
PY_TOPICS = [
    ("python", "py-var-types", "Python 变量与基本数据类型", "动态类型、int/float/str/bool、type() 与 isinstance、类型转换的坑"),
    ("python", "py-control-flow", "Python 控制流：if / for / while", "缩进即语法、for-in 与 range、break/continue/else、match-case"),
    ("python", "py-collections", "Python 内置容器：列表 / 元组 / 字典 / 集合", "各自适用场景、可变与不可变、字典遍历与 get、集合去重与运算"),
    ("python", "py-functions", "Python 函数：参数 / 作用域 / lambda", "位置与关键字参数、默认参数可变对象陷阱、*args/**kwargs、闭包"),
    ("python", "py-strings", "Python 字符串常用操作", "切片、常用方法（split/join/strip/format）、f-string、不可变性"),
    ("python", "py-oop", "Python 类与面向对象基础", "class 定义、__init__、实例与类属性、继承、魔法方法入门"),
    ("python", "py-modules", "Python 模块 / 包 / 虚拟环境", "import 机制、__name__ == '__main__'、pip 与 venv/uv、第三方库安装"),
    ("python", "py-exceptions", "Python 异常处理", "try/except/else/finally、常见异常类型、raise、自定义异常、EAFP 风格"),
    ("python", "py-file-io", "Python 文件读写", "open 与 with、文本/二进制模式、逐行迭代、编码问题"),
    ("python", "py-iter-deco", "Python 进阶基础：迭代器 / 生成器 / 装饰器", "可迭代协议、yield 与惰性求值、装饰器的本质、functools.wraps"),
]

JAVA_TOPICS = [
    ("java", "java-syntax", "Java 基本语法与数据类型", "八大基本类型、包装类与自动装箱、var、String 不可变、main 方法结构"),
    ("java", "java-control", "Java 流程控制", "if/switch（新语法）、for/for-each/while、break 与标签"),
    ("java", "java-oop", "Java 面向对象：类 / 对象 / 方法", "类定义、构造器、this、static、方法重载"),
    ("java", "java-oop-three", "Java 封装 / 继承 / 多态", "访问修饰符、extends 与 super、方法重写规则、向上转型与动态绑定"),
    ("java", "java-interface", "Java 接口与抽象类", "什么时候用哪个、default 方法、接口多实现、Comparable 示例"),
    ("java", "java-collections", "Java 集合框架：List / Set / Map", "ArrayList 与 LinkedList、HashMap 原理概览、HashSet、遍历方式与选型"),
    ("java", "java-exceptions", "Java 异常体系", "受检与非受检异常、try-with-resources、finally 执行时机、自定义异常"),
    ("java", "java-generics", "Java 泛型", "类型参数、泛型方法与泛型类、通配符 ? extends / ? super、类型擦除"),
    ("java", "java-io", "Java IO 流基础", "字节流与字符流、BufferedReader/Writer、try-with-resources 关流"),
    ("java", "java-threads", "Java 多线程基础", "Thread 与 Runnable、线程安全与 synchronized、volatile、线程池 ExecutorService 入门"),
    ("java", "java-lambda-stream", "Java Lambda 与 Stream", "函数式接口、常用 Stream 操作（filter/map/collect）、与 for 循环对比"),
]

CPP_TOPICS = [
    ("cpp", "c-syntax", "C 语言基本语法与数据类型", "变量与常量、整型家族与溢出、printf/scanf、typedef"),
    ("cpp", "c-pointer", "C 指针详解", "取地址与解引用、指针与数组、二级指针、空指针与野指针、void*"),
    ("cpp", "c-array-string", "C 数组与字符串", "一维/二维数组、char 数组与 '\\0'、string.h 常用函数、越界风险"),
    ("cpp", "c-func-struct", "C 函数与结构体", "值传递与指针传递、结构体定义与访问、typedef struct、函数指针入门"),
    ("cpp", "c-memory", "C 内存管理：malloc / free", "栈与堆的区别、malloc/calloc/realloc/free、内存泄漏与悬垂指针、检查返回值"),
    ("cpp", "c-compile", "C 编译链接与预处理", "#define 与 #include、gcc 编译四阶段、头文件守卫、声明与定义分离"),
    ("cpp", "cpp-class", "C++ 类与对象：构造 / 析构", "class 与访问控制、构造函数与初始化列表、析构时机、拷贝构造"),
    ("cpp", "cpp-ref-const", "C++ 引用与 const", "引用与指针的区别、const 指针与指向 const 的指针、const 引用传参、引用返回"),
    ("cpp", "cpp-inherit-poly", "C++ 继承与多态：虚函数", "派生类构造顺序、virtual 与虚函数表、纯虚函数与抽象类、虚析构的必要性"),
    ("cpp", "cpp-stl", "C++ STL 常用容器与算法", "vector/string/map、迭代器、sort 与常用算法函数、与手写数据结构对比"),
]

ARTICLE_PROMPT = """你是一位资深{lang}讲师。请围绕「{title}」写一篇面向初学者的技术讲解文档，它将入库 RAG 知识库供检索学习。

要求：
1. 用 markdown 格式，结构为：## 是什么 / ## 为什么重要 / ## 核心用法 / ## 代码示例 / ## 面试常问；
2. 通俗易懂，先大白话后术语；代码示例用{lang}，放在 ```{fence} 代码块里；
3. 「面试常问」部分列 3-5 条问答，每条一两行；
4. 全文 800-1500 字，不要废话，不要输出文档标题以外的任何解释性文字。

讲解侧重点：{focus}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Python/Java/C++ 基础知识文档入库")
    parser.add_argument("--python", action="store_true", help="只生成 Python 基础")
    parser.add_argument("--java", action="store_true", help="只生成 Java 基础")
    parser.add_argument("--cpp", action="store_true", help="只生成 C/C++ 基础")
    parser.add_argument("--dry-run", action="store_true", help="只列主题，不调模型不入库")
    # 模型来源：临时传参优先，缺省回退 .env 的 CHAT_*（v0.9 通常为空）
    parser.add_argument("--base-url", help="API 地址（如方舟 /api/plan），缺省用 .env")
    parser.add_argument("--api-key", help="API 密钥，缺省用 .env")
    parser.add_argument("--model", help="模型名，缺省用 .env")
    parser.add_argument("--provider", default="auto", help="openai / anthropic / auto")
    args = parser.parse_args()

    groups = []
    only = args.python or args.java or args.cpp
    if args.python or not only:
        groups += PY_TOPICS
    if args.java or not only:
        groups += JAVA_TOPICS
    if args.cpp or not only:
        groups += CPP_TOPICS
    fences = {"python": "Python", "java": "Java", "cpp": "C++"}

    print(f"共 {len(groups)} 个主题：python {len(PY_TOPICS)} / java {len(JAVA_TOPICS)} / cpp {len(CPP_TOPICS)}")
    if args.dry_run:
        for category, slug, title, _ in groups:
            print(f"  [{category}] {title} ({slug})")
        return

    db = SessionLocal()
    try:
        dao = KnowledgeDAO(db)
        llm = ChatLLM(
            provider=args.provider,
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
        )
        added = skipped = failed = 0

        for category, slug, title, focus in groups:
            source_url = f"generate://{slug}"
            if dao.get_by_url(source_url):
                skipped += 1
                print(f"跳过（已存在）: {title}")
                continue
            prompt = ARTICLE_PROMPT.format(
                lang={"cpp": "C/C++", "python": "Python", "java": "Java"}[category],
                fence=fences[category],
                title=title,
                focus=focus,
            )
            try:
                content = llm.chat(
                    [{"role": "user", "content": prompt}],
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

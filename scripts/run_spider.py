"""
分布式爬虫启动脚本。
用法（在项目根目录执行）：
    uv run python scripts/run_spider.py                 # 默认 2 个 worker，增量
    uv run python scripts/run_spider.py -w 3 --reset    # 3 个 worker，全量重爬

前置条件：Redis 和 MySQL 均已启动。
"""
import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK 编码，页面里的 emoji 会让 print 抛 UnicodeEncodeError
# （曾在异常处理里二次崩溃，直接杀死 worker 线程）。
# errors="replace" 保留中文正常显示，无法编码的字符替换为 ?
sys.stdout.reconfigure(errors="replace")
sys.stderr.reconfigure(errors="replace")

# 把 src/ 加进模块搜索路径，才能 import spider / core / database
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from spider.scheduler import SpiderScheduler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动分布式爬虫")
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=2,
        help="并发 worker 数量（默认 2）",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="全量模式：清空队列和已访问集合后重新爬取（默认增量）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scheduler = SpiderScheduler()
    scheduler.run(n_workers=args.workers, reset=args.reset)


if __name__ == "__main__":
    main()

"""前端静态服务：把 frontend/ 目录起在 10001 端口。

（原本想用 10000，但与百度网盘检测服务 YunDetectService 冲突，改用 10001）

用法（项目根目录）：
    uv run python scripts/run_frontend.py
"""
import http.server
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frontend"
PORT = 10001


def main() -> None:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"前端已启动: http://127.0.0.1:{PORT}")
    print("Ctrl+C 退出")
    server.serve_forever()


if __name__ == "__main__":
    main()

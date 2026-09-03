"""本地 mock 博查 web-search 服务（联调用）。

没有真实博查密钥、或想离线验证「联网搜索补爬」全链路时用：
把 .env 的 SEARCH_BASE_URL 指向本服务，SEARCH_API_KEY 随便填非空值即可。

设计要点：返回的候选是**真实可抓取的公开文档页**（FastAPI/Python 官方文档），
这样后续「LLM 过滤 → producer 爬取 → AI 清洗 → 向量化」全部走真实链路，
只有搜索引擎这一步是 mock 的。

用法：
    uv run python scripts/mock_bocha_server.py     # 起在 127.0.0.1:8200
    # .env:
    #   SEARCH_BASE_URL=http://127.0.0.1:8200/v1/web-search
    #   SEARCH_API_KEY=mock-key
"""
import json

from fastapi import FastAPI, Request

app = FastAPI()

# 记录最近一次请求，供测试断言（/last_request 查看）
last_request: dict = {}

# mock 候选库：真实可爬取的公开技术文档（含几个「无价值」页面验证过滤器）
MOCK_PAGES = [
    {
        "name": "FastAPI - Features",
        "url": "https://fastapi.tiangolo.com/features/",
        "snippet": "FastAPI features: high performance, fast to code, fewer bugs, intuitive, easy, short, robust, standards-based.",
        "summary": "FastAPI 官方特性页：高性能、快速编码、减少 Bug、直观、易用、简短、健壮、基于标准。",
    },
    {
        "name": "FastAPI - First Steps",
        "url": "https://fastapi.tiangolo.com/tutorial/first-steps/",
        "snippet": "The first steps with FastAPI: import FastAPI, create an app instance, write a path operation decorator.",
        "summary": "FastAPI 入门第一步：导入 FastAPI、创建 app 实例、编写路径操作装饰器与函数。",
    },
    {
        "name": "The Python Tutorial — Modules",
        "url": "https://docs.python.org/3/tutorial/modules.html",
        "snippet": "Python modules: a module is a file containing Python definitions and statements, import statements, standard modules.",
        "summary": "Python 官方教程「模块」章：模块是包含 Python 定义和语句的文件，import 用法与标准模块。",
    },
    {
        "name": "Pydantic Documentation",
        "url": "https://docs.pydantic.dev/latest/",
        "snippet": "Pydantic: data validation using Python type hints, fast and extensible core.",
        "summary": "Pydantic 官方文档：基于 Python 类型提示的数据校验，核心快速可扩展。",
    },
    # —— 以下两个是「该被过滤器剔除」的干扰项（视频页/列表页）——
    {
        "name": "【视频教程】十分钟看懂 FastAPI（某视频站）",
        "url": "https://www.bilibili.com/video/BV1mock00000",
        "snippet": "视频：十分钟带你看完 FastAPI 基础用法，弹幕互动答疑。",
        "summary": "一个视频教程页面，无文本正文。",
    },
    {
        "name": "FastAPI 相关资源下载列表",
        "url": "https://example-mock-list.invalid/fastapi-downloads",
        "snippet": "FastAPI 相关安装包与资料聚合下载列表页。",
        "summary": "下载聚合列表页，无实质内容。",
    },
]


@app.post("/v1/web-search")
async def web_search(request: Request):
    body = await request.json()
    last_request.update(
        {
            "has_auth": bool(request.headers.get("authorization")),
            "query": body.get("query"),
            "count": body.get("count"),
        }
    )
    # 按请求的 count 截断（至少给 3 条，保证过滤器有得选）
    n = max(3, min(len(MOCK_PAGES), int(body.get("count") or 8)))
    return {
        "code": 200,
        "log_id": "mock-log-1",
        "data": {"webPages": {"value": MOCK_PAGES[:n]}},
    }


# 兼容「SEARCH_BASE_URL 只配到根域」的写法
@app.post("/")
async def web_search_root(request: Request):
    return await web_search(request)


@app.get("/last_request")
async def get_last_request():
    return last_request


@app.get("/health")
async def health():
    return {"module": "mock-bocha", "status": "ok"}


if __name__ == "__main__":
    import uvicorn

    print(json.dumps({"hint": "SEARCH_BASE_URL=http://127.0.0.1:8200/v1/web-search"}, ensure_ascii=False))
    uvicorn.run(app, host="127.0.0.1", port=8200)

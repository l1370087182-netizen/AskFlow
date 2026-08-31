"""本地 mock Anthropic Messages 服务（测试用）。

模拟 POST /v1/messages 的 SSE 流式响应，用于在没有真实 Anthropic 密钥时
验证 ChatLLM 的 anthropic 协议接线（请求构造 + 流解析）。

用法：
    uv run python scripts/mock_anthropic_server.py   # 起在 127.0.0.1:8100
"""
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# 记录最近一次请求，供测试断言
last_request: dict = {}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    msgs = body.get("messages", [])
    last_request.update(
        {
            "has_api_key": bool(request.headers.get("x-api-key")),
            "has_version_header": bool(request.headers.get("anthropic-version")),
            "model": body.get("model"),
            "max_tokens": body.get("max_tokens"),
            "system": body.get("system"),
            "n_messages": len(msgs),
            "first_role": msgs[0]["role"] if msgs else None,
            "roles_alternate": all(
                msgs[i]["role"] != msgs[i + 1]["role"] for i in range(len(msgs) - 1)
            ),
        }
    )

    # 回复内容把收到的关键信息带回去，方便测试断言
    reply = (
        f"mock-ok system_len={len(body.get('system') or '')} "
        f"msgs={len(msgs)} first={msgs[0]['role'] if msgs else '-'}"
    )

    def gen():
        yield sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "mock"),
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        )
        yield sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        # 分三块流出，验证增量拼接
        for i in range(0, len(reply), max(len(reply) // 3, 1)):
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": reply[i : i + max(len(reply) // 3, 1)]},
                },
            )
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 20},
            },
        )
        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/last_request")
async def get_last_request():
    return last_request


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100)

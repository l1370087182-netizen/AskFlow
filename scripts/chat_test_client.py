"""SSE 对话测试客户端：解析 data: 事件流"""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def chat(payload: dict, show_tokens: bool = True) -> dict:
    """发起一轮对话，收集事件；返回 {meta, reply, events}"""
    meta_events = []
    reply_parts = []
    errors = []

    with httpx.Client(timeout=180) as client:
        with client.stream("POST", BASE + "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                event = json.loads(line.removeprefix("data:").strip())
                t = event["type"]
                if t == "meta":
                    meta_events.append(event)
                elif t == "token":
                    if show_tokens:
                        sys.stdout.write(event["content"])
                        sys.stdout.flush()
                    reply_parts.append(event["content"])
                elif t == "error":
                    errors.append(event["message"])
                elif t == "done":
                    break
    if show_tokens:
        print()
    return {"meta": meta_events, "reply": "".join(reply_parts), "errors": errors}


def history(session_id: str, mode: str) -> dict:
    return httpx.get(
        BASE + "/api/chat/history",
        params={"session_id": session_id, "mode": mode},
        timeout=30,
    ).json()

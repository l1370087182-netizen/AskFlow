"""会话历史存取：按会话落盘 JSON 到 SESSION_DIR。

对应 CLAUDE.md §7.4「记忆」：每个 (session_id, mode) 独立上下文，
文件名为 {session_id}_{mode}.json，结构：
    {
        "messages": [{"role": "user/assistant", "content": "..."}, ...],
        "meta": {...}   # 费曼模式存 topic / reference / rounds / evaluations
    }
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from core.config import settings

logger = logging.getLogger(__name__)

# 落盘消息上限，防止无限增长（40 条 ≈ 20 轮对话，费曼 5 轮足够）
MAX_HISTORY_MESSAGES = 40

# 同一进程内写同一文件时加锁，避免并发请求互相覆盖
_lock = threading.Lock()


def _session_path(session_id: str, mode: str) -> Path:
    """文件路径：session_id 做文件名安全清洗，两种模式各自独立文件"""
    safe_id = re.sub(r"[^\w\-.]", "_", session_id) or "default"
    d = Path(settings.SESSION_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe_id}_{mode}.json"


def load_session(session_id: str, mode: str) -> dict:
    """读会话；文件不存在或损坏时返回空会话"""
    path = _session_path(session_id, mode)
    if not path.exists():
        return {"messages": [], "meta": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 结构兜底，防止旧文件缺字段
        data.setdefault("messages", [])
        data.setdefault("meta", {})
        return data
    except Exception as e:  # noqa: BLE001 —— 坏了就按新会话处理
        logger.warning("[session] 会话文件损坏，重新初始化：%s", e)
        return {"messages": [], "meta": {}}


def save_session(session_id: str, mode: str, data: dict) -> None:
    """写会话；只保留最近 MAX_HISTORY_MESSAGES 条消息"""
    data["messages"] = data["messages"][-MAX_HISTORY_MESSAGES:]
    path = _session_path(session_id, mode)
    with _lock:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def append_messages(session_id: str, mode: str, *pairs: tuple[str, str]) -> dict:
    """追加若干 (role, content) 消息并落盘，返回最新会话数据"""
    data = load_session(session_id, mode)
    for role, content in pairs:
        data["messages"].append({"role": role, "content": content})
    save_session(session_id, mode, data)
    return data


def delete_session_files(session_id: str, mode: str | None = None) -> int:
    """删除会话落盘文件，返回删除个数

    mode 为 None 时删 ask/teach 两个文件；指定 mode 时只删该模式。
    """
    modes = (mode,) if mode in ("ask", "teach") else ("ask", "teach")
    deleted = 0
    for m in modes:
        p = _session_path(session_id, m)
        if p.exists():
            p.unlink()
            deleted += 1
    return deleted

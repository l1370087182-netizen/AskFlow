"""双模式对话接口（阶段 7）：

    POST /api/chat          SSE 流式对话，mode=ask 讲解 / mode=teach 费曼
    GET  /api/chat/history  拉取会话历史

讲解模式：用户提问 → 混合检索 top5 → 组装提示 → 流式讲解
费曼模式：选题（检索参考答案，藏进系统提示）→ 逐轮追问（最多 N 轮）
          → 结束（关键词/finish 标志/轮次用尽）→ 总结复述 + 评分
"""
from __future__ import annotations

import json
import logging
from collections.abc import Generator
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from core.config import settings
from database.session import SessionLocal
from evaluate.evaluator import save_evaluation
from generation.chain import ChainBuilder, format_context
from generation.llm import ChatLLM, build_llm_for_user
from generation.prompts import FINISH_KEYWORDS, MAX_TEACH_ROUNDS
from model.UserModel import UserModel
from schema.chat import ChatRequest, LLMConfig, PingRequest, UndoRequest
from util.session_store import (
    delete_session_files,
    list_user_session_files,
    load_session,
    save_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["对话"])


# ---------- 工具 ----------

def _sse(event: dict) -> str:
    """SSE 事件行：data: {json}\\n\\n"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _build_llm(cfg: LLMConfig | None) -> ChatLLM:
    """按请求里透传的自定义配置构造 ChatLLM（现仅 /ping 探测未保存配置用）"""
    if cfg and cfg.base_url.strip() and cfg.api_key.strip():
        provider = cfg.provider or "auto"
        model = cfg.model.strip()
        if not model:
            resolved = ChatLLM._resolve_provider(provider, cfg.base_url)
            model = "claude-opus-4-8" if resolved == "anthropic" else settings.CHAT_MODEL
        return ChatLLM(
            provider=provider,
            base_url=cfg.base_url.strip(),
            api_key=cfg.api_key.strip(),
            model=model,
        )
    return ChatLLM()


def _stream_events(
    llm: ChatLLM,
    messages: list[dict],
    temperature: float,
    out: list[str],
) -> Generator[str, None, None]:
    """逐块产出 token 事件，同时把文本累积进 out（生成器结束后拼出完整回复）"""
    for piece in llm.stream_chat(messages, temperature):
        out.append(piece)
        yield _sse({"type": "token", "content": piece})


# ---------- 讲解模式 ----------

def _chat_ask(
    db: Session, body: ChatRequest, llm: ChatLLM, chain: ChainBuilder, uid: int
) -> Generator[str, None, None]:
    """讲解模式：每条消息即时检索，基于片段流式回答"""
    session = load_session(uid, body.session_id, "ask")

    # 个人知识库：检索全局块 + 本人个人块；传入 llm 启用编排检索（可降级）
    messages, results = chain.build_ask(body.message, session["messages"], uid=uid, llm=llm)

    # 先把引用来源推给前端（片段对应的知识条目），再开始流式正文
    yield _sse(
        {
            "type": "meta",
            "mode": "ask",
            "sources": [
                {
                    "knowledge_id": r["knowledge_id"],
                    "category": r["category"],
                    "score": round(r.get("score", 0.0), 4),
                }
                for r in results
            ],
        }
    )

    out: list[str] = []
    yield from _stream_events(llm, messages, temperature=0.4, out=out)
    reply = "".join(out)

    # 落盘会话记忆（用户消息 + 助手回复）
    session["messages"].append({"role": "user", "content": body.message})
    session["messages"].append({"role": "assistant", "content": reply})
    save_session(uid, body.session_id, "ask", session)

    yield _sse({"type": "done"})


# ---------- 费曼模式 ----------

def _chat_teach(
    db: Session, body: ChatRequest, llm: ChatLLM, chain: ChainBuilder, uid: int
) -> Generator[str, None, None]:
    """费曼模式：选题 → 追问 → 总结评分 三阶段状态机"""
    session = load_session(uid, body.session_id, "teach")
    meta = session["meta"]
    msg = body.message.strip()
    is_finish = body.finish or msg.lower() in FINISH_KEYWORDS

    # ---- 阶段 1：选题（meta 还没有 topic）----
    if "topic" not in meta:
        # 新主题开始：清空上一段已结束对话的上下文（旧对话已落盘，仅在开新主题时重置）
        session["messages"] = []
        topic = (body.topic or msg).strip()
        # 检索参考答案：藏进系统提示当「标准答案」，不直接展示给用户
        # 个人知识库：费曼选题同样能选到本人的个人知识
        results = chain.retriever.search(topic, top_k=5, uid=uid)
        meta.update({"topic": topic, "reference": format_context(results), "rounds": 0})
        save_session(uid, body.session_id, "teach", session)

        if not body.topic:
            # 未显式传 topic：本轮消息就是主题本身，只做开场，不算讲解内容
            yield _sse(
                {
                    "type": "meta",
                    "mode": "teach",
                    "stage": "opening",
                    "topic": topic,
                    "rounds": 0,
                    "max_rounds": MAX_TEACH_ROUNDS,
                }
            )
            out: list[str] = []
            yield from _stream_events(
                llm, chain.build_teach_opening(session), temperature=0.7, out=out
            )
            session["messages"].append({"role": "assistant", "content": "".join(out)})
            save_session(uid, body.session_id, "teach", session)
            yield _sse({"type": "done"})
            return
        # 显式传了 topic：当前消息是第一段讲解，继续往下走追问逻辑

    # ---- 内部：评分流（阶段2手动结束 与 阶段3满轮自动 共用）----
    def _eval_stream(include_message: bool) -> Generator[str, None, None]:
        cur_rounds = meta.get("rounds", 0)
        messages = chain.build_teach(
            session, msg, finish=True, include_message=include_message
        )
        out_e: list[str] = []
        yield from _stream_events(llm, messages, temperature=0.3, out=out_e)
        evaluation = "".join(out_e)

        meta.setdefault("evaluations", []).append(
            {"topic": meta["topic"], "rounds": cur_rounds, "evaluation": evaluation}
        )
        try:
            save_evaluation(
                db,
                user_id=uid,
                session_id=body.session_id,
                topic=meta["topic"],
                rounds=cur_rounds,
                evaluation_text=evaluation,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[chat] 评分落库失败")
        # 保留完整对话历史（持久化，侧边栏可见）；只清主题状态以便下次开新主题
        for key in ("topic", "reference", "rounds"):
            meta.pop(key, None)
        save_session(uid, body.session_id, "teach", session)

    # ---- 阶段 2：主动结束 → 评分 ----
    rounds = meta.get("rounds", 0)
    if is_finish or rounds >= MAX_TEACH_ROUNDS:
        yield _sse(
            {
                "type": "meta",
                "mode": "teach",
                "stage": "evaluation",
                "topic": meta["topic"],
                "rounds": rounds,
            }
        )
        include_message = not (body.finish or msg.lower() in FINISH_KEYWORDS)
        yield from _eval_stream(include_message)
        yield _sse({"type": "done"})
        return

    # ---- 第 5 轮（最后一条）回答：点评+直接总评，不再抛新问题 ----
    if rounds + 1 >= MAX_TEACH_ROUNDS:
        yield _sse(
            {
                "type": "meta",
                "mode": "teach",
                "stage": "evaluation",
                "topic": meta["topic"],
                "rounds": rounds + 1,
            }
        )
        yield from _eval_stream(include_message=True)
        yield _sse({"type": "done"})
        return

    # ---- 阶段 3：常规追问轮（点评+下一个问题）----
    yield _sse(
        {
            "type": "meta",
            "mode": "teach",
            "stage": "questioning",
            "topic": meta["topic"],
            "rounds": rounds,
            "max_rounds": MAX_TEACH_ROUNDS,
        }
    )
    messages = chain.build_teach(session, msg)
    out3: list[str] = []
    yield from _stream_events(llm, messages, temperature=0.7, out=out3)
    reply = "".join(out3)

    session["messages"].append({"role": "user", "content": msg})
    session["messages"].append({"role": "assistant", "content": reply})
    meta["rounds"] = rounds + 1
    save_session(uid, body.session_id, "teach", session)

    yield _sse({"type": "done"})


# ---------- 路由 ----------

@router.post("")
def chat(body: ChatRequest, user: UserModel = Depends(get_current_user)):
    """双模式对话（SSE 流式）

    事件类型：
        meta        —— 开始元信息（模式/阶段/主题/引用来源等）
        token       —— 模型增量输出 {"content": "..."}
        eval_start  —— 费曼满轮自动总评：前端据此切一个新气泡接收总评
        done        —— 本轮结束
        error       —— 异常 {"message": "..."}
    """
    # 只捕获 uid（int），不把 ORM 对象带进生成器（避免跨 Session detached）
    uid = user.id

    def generate() -> Generator[str, None, None]:
        # 流式响应不能用 Depends(get_db)：
        # 端点函数返回时依赖就会关闭，而流才刚开始。改为手动管理会话。
        db = SessionLocal()
        # 用户私有模型配置优先；未配置回退服务端默认模型（对话不挂）
        llm = build_llm_for_user(db, uid) or ChatLLM()
        try:
            chain = ChainBuilder(db)
            if body.mode == "ask":
                yield from _chat_ask(db, body, llm, chain, uid)
            else:
                yield from _chat_teach(db, body, llm, chain, uid)
        except Exception as e:  # noqa: BLE001 —— SSE 里异常也要发给前端
            logger.exception("[chat] 对话异常")
            yield _sse({"type": "error", "message": f"对话失败：{e}"})
        finally:
            db.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 关闭 Nginx 等反代缓冲，保证逐块到达
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/history")
def history(
    session_id: str = Query(default="default", max_length=64),
    mode: Literal["ask", "teach"] = Query(default="ask"),
    user: UserModel = Depends(get_current_user),
):
    """拉取会话历史（只读本用户目录；meta 中不外泄费曼模式的参考答案）"""
    session = load_session(user.id, session_id, mode)
    meta = session.get("meta", {})
    safe_meta = {k: v for k, v in meta.items() if k != "reference"}
    return {
        "session_id": session_id,
        "mode": mode,
        "messages": session["messages"],
        "meta": safe_meta,
    }


@router.post("/undo")
def undo_message(body: UndoRequest, user: UserModel = Depends(get_current_user)):
    """撤回上一轮：删除最后一组「用户消息 + 紧随的助手回复」，返回用户原文。

    误触回车的补救：只撤这一轮，不动更早的历史；用户原文回填输入框续写。
    费曼模式同步把轮次计数退回一格。会话为空/无可撤项返回 400。
    """
    session = load_session(user.id, body.session_id, body.mode)
    msgs = session["messages"]

    # 从尾部定位最后一条助手回复
    ai_idx = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant":
            ai_idx = i
            break
    # 助手回复前必须紧挨一条用户消息才构成可撤的「一问一答」
    if ai_idx is None or ai_idx == 0 or msgs[ai_idx - 1].get("role") != "user":
        raise HTTPException(status_code=400, detail="没有可撤回的消息")

    restored = msgs[ai_idx - 1].get("content", "")
    del msgs[ai_idx - 1 : ai_idx + 1]

    # 费曼/面试会话带轮次计数，撤回同步退回一格
    meta = session.get("meta", {})
    if body.mode in ("teach", "interview") and isinstance(meta.get("rounds"), int) and meta["rounds"] > 0:
        meta["rounds"] -= 1

    save_session(user.id, body.session_id, body.mode, session)
    return {"restored": restored, "remaining": len(msgs)}


@router.post("/ping")
def ping_llm(body: PingRequest):
    """模型连通性测试：用用户配置发一句极简对话，成功返回 ok

    不写任何会话记录，纯探测地址/密钥/模型是否可用（测的是弹窗里未保存的配置）。
    """
    try:
        llm = _build_llm(body.llm)
        reply = llm.chat(
            [{"role": "user", "content": "请只回复两个字：你好"}], temperature=0.1
        )
        return {
            "ok": True,
            "provider": llm.provider,
            "model": llm.model,
            "reply": reply.strip()[:30],
        }
    except Exception as e:  # noqa: BLE001 —— 把失败原因带回前端展示
        logger.warning("[chat] ping 失败：%s", e)
        return {"ok": False, "error": str(e)[:300]}


@router.get("/sessions")
def list_sessions(user: UserModel = Depends(get_current_user)):
    """会话列表：只扫当前用户目录，按 (会话, 模式) 拆分，讲解/费曼各自独立

    每个落盘文件 {sid}_{mode}.json 就是一条会话记录，标题取该模式第一条用户消息。
    """
    items: list[dict] = []

    for f in list_user_session_files(user.id):
        sid, _, mode = f.stem.rpartition("_")
        if mode not in ("ask", "teach") or not sid:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 坏文件跳过
            continue
        msgs = data.get("messages", [])
        if not msgs:  # 空会话不进列表
            continue
        first = next(
            (m["content"] for m in msgs if m.get("role") == "user"), ""
        )
        items.append(
            {
                "session_id": sid,
                "mode": mode,
                "title": (first.strip().replace("\n", " ")[:30]) or "新对话",
                "messages": len(msgs),
                "updated": f.stat().st_mtime,
            }
        )

    items.sort(key=lambda x: x["updated"], reverse=True)
    for it in items[:50]:
        it["updated"] = datetime.fromtimestamp(it["updated"]).isoformat()
    return {"total": len(items), "sessions": items[:50]}


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    mode: Literal["ask", "teach"] | None = Query(default=None),
    user: UserModel = Depends(get_current_user),
):
    """删除会话（只删本用户目录）：mode 指定时只删该模式，否则两种模式都删"""
    deleted = delete_session_files(user.id, session_id, mode)
    return {"session_id": session_id, "deleted": deleted}

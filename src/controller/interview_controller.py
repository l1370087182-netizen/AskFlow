"""模拟面试接口（JD + 简历双图）：

    POST /api/interview/start   上传 JD+简历截图 → OCR → 分析 → 首个问题
    POST /api/interview/answer  SSE：逐轮点评+追问；结束→总评+推荐学习卡片

会话存 sessions/{id}_interview.json，meta 里存 jd_analysis/resume/rounds。
"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Generator
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from DAO.agent_task_dao import AgentTaskDAO
from DAO.jd_dao import JDDAO
from DAO.tech_term_dao import TechTermDAO
from database.session import SessionLocal, get_db
from generation.llm import ChatLLM, build_llm_for_user
from interview.analyzer import InterviewAnalyzer, extract_weaknesses
from model.AgentTaskModel import TaskKind
from model.InterviewRecordModel import InterviewRecordModel
from interview.prompts import (
    FINAL_ASSESS_PROMPT,
    FIRST_QUESTION_PROMPT,
    INTERVIEWER_PROMPT,
)
from jd_analyzer.analyzer import JDAnalyzer
from model.UserModel import UserModel
from ocr.ocr_client import OCRClient, build_ocr_client_for_user
from util.session_store import load_session, save_session

router = APIRouter(prefix="/api/interview", tags=["模拟面试"])

MAX_ROUNDS = 5
ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _ocr(file: UploadFile, client: OCRClient) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/jpeg/webp/bmp 图片")
    return client.recognize(file.file.read())


def _system(jd_analysis: dict, resume: dict, rounds: int) -> str:
    a = InterviewAnalyzer()
    return INTERVIEWER_PROMPT.format(
        jd_points=a.jd_points_text(jd_analysis),
        resume_brief=a.resume_brief_text(resume),
        round_no=rounds + 1,
        max_rounds=MAX_ROUNDS,
    )


def _recommend(gap: list[str], db: Session) -> list[dict]:
    """把差距主题映射到知识卡片（tech_term 模糊匹配）"""
    dao = TechTermDAO(db)
    terms = dao.list_all()
    recs = []
    for topic in gap:
        t = topic.lower()
        hit = next(
            (x for x in terms if t in x.term.lower() or x.term.lower() in t), None
        )
        recs.append(
            {
                "topic": topic,
                "term": hit.term if hit else None,
                "brief": hit.brief if hit else "",
            }
        )
    return recs


@router.post("/start")
def start(
    jd: UploadFile = File(..., description="JD 截图"),
    resume: UploadFile = File(..., description="简历截图"),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """上传双图，返回面试会话与第一个问题（会话存当前用户目录）。

    每一步异常都兜成 JSON 错误（含真实原因），杜绝裸 500 纯文本。
    """
    uid = user.id

    # 1) OCR：视觉模型优先用 ⚙️ 个人配置（与对话同源），未配置回退服务端 OCR_*
    try:
        ocr_client = build_ocr_client_for_user(db, uid)
        jd_text = _ocr(jd, ocr_client)
        resume_text = _ocr(resume, ocr_client)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(
                f"截图文字识别（OCR）失败：{e}。"
                "请确认 ⚙️ 个人模型（或服务端 OCR_MODEL）可用且支持图片输入"
            ),
        ) from e
    if not jd_text.strip():
        raise HTTPException(status_code=422, detail="JD 未识别出文字")

    # 2) LLM：与对话一致——用户个人配置优先，未配置回退服务端默认
    try:
        llm = build_llm_for_user(db, uid) or ChatLLM()
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"{e}；可到「对话学习」页 ⚙️ 配置个人模型",
        ) from e

    # 3) JD 分析 + 简历结构化
    try:
        jd_analysis = JDAnalyzer(llm).analyze(jd_text)
        resume = InterviewAnalyzer(llm).extract_resume(resume_text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"JD/简历分析失败：{e}") from e

    # 3.5) JD 分析落库（复用 jd 表，面试记录用 jd_id 关联；失败不阻塞面试）
    jd_id = None
    try:
        jd_row = JDDAO(db).create(user_id=uid, filename="interview_jd", image_path="")
        JDDAO(db).save_result(
            jd_row.id,
            ocr_text=jd_text,
            title=jd_analysis.get("title", ""),
            summary=jd_analysis.get("summary", ""),
            analysis_raw=str(jd_analysis),
            tech_stack=jd_analysis.get("tech_stack", []),
        )
        jd_id = jd_row.id
    except Exception as e:  # noqa: BLE001
        logger.warning("[interview] JD 分析落库失败（面试继续）：%s", e)

    # 强随机 id（弃用秒级时间戳，消除同秒碰撞；目录隔离后他人也无法访问）
    session_id = f"iv-{secrets.token_urlsafe(12)}"
    save_session(
        uid,
        session_id,
        "interview",
        {
            "messages": [],
            "meta": {
                "jd_analysis": jd_analysis,
                "resume": resume,
                "rounds": 0,
                "jd_id": jd_id,
            },
        },
    )

    # 4) 首问生成
    try:
        first = llm.chat(
            [
                {"role": "system", "content": _system(jd_analysis, resume, 0)},
                {"role": "user", "content": FIRST_QUESTION_PROMPT},
            ],
            temperature=0.5,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"模型调用失败：{e}") from e
    data = load_session(uid, session_id, "interview")
    data["messages"].append({"role": "assistant", "content": first})
    save_session(uid, session_id, "interview", data)

    return {
        "session_id": session_id,
        "title": jd_analysis.get("title", ""),
        "tech_stack": jd_analysis.get("tech_stack", []),
        "resume_skills": resume.get("skills", []),
        "first_question": first,
    }


class AnswerRequest(BaseModel):
    session_id: str
    message: str = Field(default="")
    finish: bool = False


@router.post("/answer")
def answer(body: AnswerRequest, user: UserModel = Depends(get_current_user)):
    """SSE：点评+追问；结束→总评+推荐（只读本用户的面试会话）"""
    uid = user.id

    def generate() -> Generator[str, None, None]:
        db = SessionLocal()
        try:
            # 个人配置优先；构造放 try 内，配置缺失走 SSE error 而非裸 500
            llm = build_llm_for_user(db, uid) or ChatLLM()
            session = load_session(uid, body.session_id, "interview")
            meta = session.get("meta", {})
            jd_analysis = meta.get("jd_analysis", {})
            resume = meta.get("resume", {})
            rounds = meta.get("rounds", 0)

            # 会话不存在（拿别人的 id / 已结束重置 / 根本没开始）：明确报错，
            # 而不是拿空 meta 去调 LLM
            if not jd_analysis:
                yield _sse({"type": "error", "message": "面试会话不存在或已结束"})
                return

            if body.finish or rounds >= MAX_ROUNDS:
                # 总评
                msgs = [
                    {"role": "system", "content": _system(jd_analysis, resume, rounds)},
                    *session["messages"],
                    {"role": "user", "content": FINAL_ASSESS_PROMPT},
                ]
                out: list[str] = []
                for piece in llm.stream_chat(msgs, temperature=0.4):
                    out.append(piece)
                    yield _sse({"type": "token", "content": piece})
                final_text = "".join(out)
                session["messages"].append({"role": "assistant", "content": final_text})

                # 推荐学习卡片（JD required 且简历未覆盖）
                gap = InterviewAnalyzer.compute_gap(jd_analysis, resume)
                yield _sse({"type": "recs", "items": _recommend(gap, db)})

                # 面试记录落库（学习规划/任务板的数据源；失败不阻塞收尾）
                try:
                    rec = InterviewRecordModel(
                        user_id=uid,
                        jd_id=meta.get("jd_id"),
                        jd_title=jd_analysis.get("title", ""),
                        rounds=rounds,
                        final_summary=final_text,
                        weaknesses=extract_weaknesses(final_text, llm),
                        gap_topics=gap,
                        resume_skills=resume.get("skills", []),
                        transcript=session["messages"],
                    )
                    db.add(rec)
                    db.commit()
                    yield _sse({"type": "record", "record_id": rec.id})
                except Exception as e:  # noqa: BLE001
                    logger.warning("[interview] 面试记录落库失败：%s", e)

                save_session(uid, body.session_id, "interview", {"messages": [], "meta": {}})
                yield _sse({"type": "done"})
                return

            # 点评+追问
            msgs = [
                {"role": "system", "content": _system(jd_analysis, resume, rounds)},
                *session["messages"],
                {"role": "user", "content": body.message},
            ]
            out2: list[str] = []
            for piece in llm.stream_chat(msgs, temperature=0.5):
                out2.append(piece)
                yield _sse({"type": "token", "content": piece})
            session["messages"].append({"role": "user", "content": body.message})
            session["messages"].append({"role": "assistant", "content": "".join(out2)})
            meta["rounds"] = rounds + 1
            save_session(uid, body.session_id, "interview", session)
            yield _sse({"type": "round", "rounds": rounds + 1, "max_rounds": MAX_ROUNDS})
            yield _sse({"type": "done"})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"面试失败：{e}"})
        finally:
            db.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 面试记录与学习计划 ----------

def _get_record(db: Session, user_id: int, record_id: int) -> InterviewRecordModel:
    """面试记录属主校验：越权/不存在一律 404（隐藏存在性）"""
    rec = (
        db.query(InterviewRecordModel)
        .filter(InterviewRecordModel.id == record_id)
        .first()
    )
    if not rec or rec.user_id != user_id:
        raise HTTPException(status_code=404, detail="未找到该面试记录")
    return rec


@router.get("/records")
def list_records(
    limit: int = 20,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """面试记录列表（仅本人，按时间倒序）"""
    rows = (
        db.query(InterviewRecordModel)
        .filter(InterviewRecordModel.user_id == user.id)
        .order_by(InterviewRecordModel.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "jd_title": r.jd_title,
                "rounds": r.rounds,
                "weaknesses": len(r.weaknesses or []),
                "gaps": len(r.gap_topics or []),
                "has_plan": bool(r.plan_task_id),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/records/{record_id}")
def get_record(
    record_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """面试记录详情（总评/弱项/缺口/逐轮，仅本人）"""
    rec = _get_record(db, user.id, record_id)
    return {
        "id": rec.id,
        "jd_title": rec.jd_title,
        "rounds": rec.rounds,
        "final_summary": rec.final_summary,
        "weaknesses": rec.weaknesses or [],
        "gap_topics": rec.gap_topics or [],
        "resume_skills": rec.resume_skills or [],
        "transcript": rec.transcript or [],
        "plan_task_id": rec.plan_task_id,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


@router.post("/records/{record_id}/plan")
def request_plan(
    record_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """为面试记录生成学习计划：发 study_plan 任务，PlannerAgent 异步执行。

    幂等：该记录已有进行中/待认领的计划任务时直接复用，不重复派发。
    """
    rec = _get_record(db, user.id, record_id)
    dao = AgentTaskDAO(db)

    # 复用已有未完成的计划任务（幂等，防重复点击）
    if rec.plan_task_id:
        existing = dao.get(rec.plan_task_id)
        if existing and existing.status in ("pending", "in_progress"):
            return {"task_id": existing.id, "status": existing.status, "reused": True}

    task = dao.create(
        kind=TaskKind.STUDY_PLAN,
        user_id=user.id,
        payload={"interview_record_id": rec.id},
        agent="api",
    )
    rec.plan_task_id = task.id
    db.commit()
    return {"task_id": task.id, "status": "pending", "reused": False}


@router.get("/records/{record_id}/plan")
def get_plan(
    record_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """查询该记录的学习计划任务状态与结果"""
    rec = _get_record(db, user.id, record_id)
    if not rec.plan_task_id:
        return {"status": "none", "output": None}
    task = AgentTaskDAO(db).get(rec.plan_task_id)
    if not task:
        return {"status": "none", "output": None}
    return {"status": task.status, "output": task.output or {}}

"""模拟面试接口（JD + 简历双图）：

    POST /api/interview/start   上传 JD+简历截图 → OCR → 分析 → 首个问题
    POST /api/interview/answer  SSE：逐轮点评+追问；结束→总评+推荐学习卡片

会话存 sessions/{id}_interview.json，meta 里存 jd_analysis/resume/rounds。
"""
from __future__ import annotations

import json
import time
from collections.abc import Generator
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from DAO.jd_dao import JDDAO
from DAO.tech_term_dao import TechTermDAO
from database.session import SessionLocal, get_db
from generation.llm import ChatLLM
from interview.analyzer import InterviewAnalyzer
from interview.prompts import (
    FINAL_ASSESS_PROMPT,
    FIRST_QUESTION_PROMPT,
    INTERVIEWER_PROMPT,
)
from jd_analyzer.analyzer import JDAnalyzer
from ocr.ocr_client import OCRClient
from util.session_store import load_session, save_session

router = APIRouter(prefix="/api/interview", tags=["模拟面试"])

MAX_ROUNDS = 5
ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _ocr(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/jpeg/webp/bmp 图片")
    return OCRClient().recognize(file.file.read())


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
):
    """上传双图，返回面试会话与第一个问题"""
    jd_text = _ocr(jd)
    resume_text = _ocr(resume)
    if not jd_text.strip():
        raise HTTPException(status_code=422, detail="JD 未识别出文字")

    llm = ChatLLM()
    jd_analysis = JDAnalyzer().analyze(jd_text)
    resume = InterviewAnalyzer(llm).extract_resume(resume_text)

    session_id = f"iv-{int(time.time())}"
    save_session(
        session_id,
        "interview",
        {"messages": [], "meta": {"jd_analysis": jd_analysis, "resume": resume, "rounds": 0}},
    )

    analyzer = InterviewAnalyzer()
    first = llm.chat(
        [
            {"role": "system", "content": _system(jd_analysis, resume, 0)},
            {"role": "user", "content": FIRST_QUESTION_PROMPT},
        ],
        temperature=0.5,
    )
    data = load_session(session_id, "interview")
    data["messages"].append({"role": "assistant", "content": first})
    save_session(session_id, "interview", data)

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
def answer(body: AnswerRequest):
    """SSE：点评+追问；结束→总评+推荐"""

    def generate() -> Generator[str, None, None]:
        db = SessionLocal()
        llm = ChatLLM()
        try:
            session = load_session(body.session_id, "interview")
            meta = session.get("meta", {})
            jd_analysis = meta.get("jd_analysis", {})
            resume = meta.get("resume", {})
            rounds = meta.get("rounds", 0)

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
                session["messages"].append({"role": "assistant", "content": "".join(out)})

                # 推荐学习卡片（JD required 且简历未覆盖）
                gap = InterviewAnalyzer.compute_gap(jd_analysis, resume)
                yield _sse({"type": "recs", "items": _recommend(gap, db)})
                save_session(body.session_id, "interview", {"messages": [], "meta": {}})
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
            save_session(body.session_id, "interview", session)
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

"""JD 分析接口（阶段 8）：

    POST /api/jd/analyze   上传 JD 截图 → OCR → 技术栈提取 → 落库
    GET  /api/jd/{jd_id}   查询单条分析结果
    GET  /api/jd/          分析记录列表
"""
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from DAO.jd_dao import JDDAO
from auth.deps import get_current_user
from database.session import get_db
from generation.llm import build_llm_for_user
from jd_analyzer.analyzer import JDAnalyzer
from model.UserModel import UserModel
from ocr.ocr_client import OCRClient
from schema.jd import JDAnalyzeResponse, JDItem, JDListResponse, TechStackItem

router = APIRouter(prefix="/api/jd", tags=["JD分析"])

# 只收常见图片格式
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@router.post("/analyze", response_model=JDAnalyzeResponse)
def analyze_jd(
    file: UploadFile = File(..., description="JD 截图"),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """上传 JD 截图，走 截图落盘 → OCR → LLM 提取技术栈 → 落库 全流程

    同步 def：内部是阻塞式 HTTP 调用（OCR、LLM），交给线程池执行。
    """
    # 1) 校验格式
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/jpeg/webp/bmp 图片")

    # 同步端点里 UploadFile.read() 是异步的，用底层 .file 同步读取
    image_bytes = file.file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="文件为空")

    # 2) 截图落盘到 storage/jd/（CLAUDE.md 约定 storage/ 存 JD 截图；时间戳前缀防覆盖）
    jd_dir = Path("storage") / "jd"
    jd_dir.mkdir(parents=True, exist_ok=True)
    image_path = jd_dir / f"{int(time.time())}_{filename}"
    image_path.write_bytes(image_bytes)

    dao = JDDAO(db)
    jd = dao.create(user_id=user.id, filename=filename, image_path=str(image_path))

    try:
        # 3) OCR 识别截图文字
        ocr = OCRClient()
        ocr_text = ocr.recognize(image_bytes, mime=MIME_MAP[suffix])
        if not ocr_text.strip():
            raise HTTPException(status_code=422, detail="OCR 未识别出任何文字")

        # 4) LLM 提取结构化技术栈（个人配置优先，未配置回退服务端默认）
        analyzer = JDAnalyzer(build_llm_for_user(db, user.id))
        result = analyzer.analyze(ocr_text)

        # 5) 落库
        dao.save_result(
            jd.id,
            ocr_text=ocr_text,
            title=result["title"],
            summary=result["summary"],
            analysis_raw=str(result),
            tech_stack=result["tech_stack"],
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 —— 给用户明确错误而不是 500
        raise HTTPException(status_code=500, detail=f"JD 分析失败：{e}") from e

    return JDAnalyzeResponse(
        jd_id=jd.id,
        title=result["title"],
        summary=result["summary"],
        ocr_text=ocr_text,
        tech_stack=[TechStackItem(**item) for item in result["tech_stack"]],
        soft_requirements=result["soft_requirements"],
    )


@router.get("/{jd_id}", response_model=JDAnalyzeResponse)
def get_jd(
    jd_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """查询单条分析结果（越权查询返回 404，不泄露存在性）"""
    dao = JDDAO(db)
    jd = dao.get_by_id(jd_id, user.id)
    if not jd:
        raise HTTPException(status_code=404, detail="未找到该 JD 记录")
    stack = dao.get_stack(jd_id)
    return JDAnalyzeResponse(
        jd_id=jd.id,
        title=jd.title,
        summary=jd.summary,
        ocr_text=jd.ocr_text,
        tech_stack=[TechStackItem.model_validate(s) for s in stack],
        soft_requirements=[],  # 软性要求只在分析响应里返回，不再单独存表
    )


@router.get("/", response_model=JDListResponse)
def list_jd(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """分析记录列表（仅当前用户）"""
    dao = JDDAO(db)
    rows = dao.list_recent(user.id, limit=limit, offset=offset)
    return JDListResponse(
        total=len(rows),
        items=[JDItem.model_validate(r) for r in rows],
    )

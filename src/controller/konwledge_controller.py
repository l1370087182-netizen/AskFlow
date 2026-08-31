from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from DAO.knowledge_dao import KnowledgeDAO
from database.session import get_db
from core.config import settings
from schema.knowledge import (
    KnowledgeItem,
    KnowledgeListResponse,
    KnowledgeCreateRequest,
    KnowledgeDetailResponse,
)

router = APIRouter(prefix="/api/knowledge", tags=["知识库"])

# 上传只允许这两种纯文本格式
ALLOWED_SUFFIXES = {".md", ".txt"}

@router.get("/health")
async def health():
    """健康检查"""
    return {"module": "knowledge", "status": "ok"}

@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    """按分类聚合知识数量（知识库浏览页用）"""
    from sqlalchemy import func as sa_func
    from model.KnowledgeModel import KnowledgeModel

    rows = (
        db.query(
            KnowledgeModel.category,
            sa_func.count(KnowledgeModel.id),
        )
        .group_by(KnowledgeModel.category)
        .order_by(sa_func.count(KnowledgeModel.id).desc())
        .all()
    )
    return {
        "total": sum(n for _, n in rows),
        "categories": [
            {"category": c, "count": n} for c, n in rows
        ],
    }

@router.get("/{knowledge_id}", response_model=KnowledgeDetailResponse)
def knowledge_detail(knowledge_id: int, db: Session = Depends(get_db)):
    """单条知识详情（含正文，知识库页弹窗展示）"""
    row = KnowledgeDAO(db).get_by_db(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到该知识")
    return KnowledgeDetailResponse.model_validate(row)

@router.get("/", response_model=KnowledgeListResponse)
async def list_knowledge(
    category: str | None = None,
    status: int | None = None,
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """分页查询知识列表"""
    dao = KnowledgeDAO(db)

    rows = dao.list_by_category(
        category=category,
        status=status,
        limit=limit,
        offset=offset,
    )
    item = [KnowledgeItem.model_validate(r) for r in rows]
    return KnowledgeListResponse(
        total=len(rows),
        items=item,
    )

@router.post("/", response_model=KnowledgeItem)
async def create_knowledge(
    body: KnowledgeCreateRequest,
    db: Session = Depends(get_db),
):
    """手动创建知识"""
    dao = KnowledgeDAO(db)
    row = dao.create(
        title=body.title,
        content=body.content,
        source_url=body.source_url,
        category=body.category,
        source_type=body.source_type,
    )
    if row is None:
        raise HTTPException(status_code=409, detail="知识已存在")
    return KnowledgeItem.model_validate(row)


@router.post("/upload", response_model=KnowledgeItem)
async def upload_knowledge(
    file: UploadFile = File(..., description="要上传的 md/txt 文档"),
    title: str | None = Form(default=None, description="标题，不传则用文件名"),
    category: str = Form(default="general", description="技术分类"),
    db: Session = Depends(get_db),
):
    """文档上传入库：读取文本 → 校验 → upsert 进 knowledge 表（source_type=upload）"""

    # 1) 少数客户端可能不带文件名，先兜底
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    # 2) 扩展名校验：只收 md/txt
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 .md / .txt 文件")

    # 3) 读取并解码；学习阶段文档都不大，直接全量读取，编码统一要求 UTF-8
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件必须是 UTF-8 编码")

    # 4) 与爬虫同一规则：正文太短视为无效，不入库
    content = content.strip()
    if len(content) < 200:
        raise HTTPException(status_code=400, detail="正文少于 200 字，视为无效")

    # 5) 标题缺省用文件名（去掉扩展名）
    final_title = title.strip() if title else Path(filename).stem

    # 6) 构造伪 URL 作为唯一键：source_url 是 NOT NULL + UNIQUE，
    #    多份上传都填空串会撞唯一约束；用文件名做键，同名重传正好走 upsert 更新分支
    source_url = f"upload://{filename}"

    # 7) 原文件落盘到 DATA_DIR/uploads，便于追溯；同名直接覆盖（同名=同一份文档）
    upload_dir = Path(settings.DATA_DIR) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / filename).write_bytes(raw)

    # 8) 复用 DAO 的 upsert：新文档插入；同名文档更新且 status 重置为待向量化
    dao = KnowledgeDAO(db)
    row = dao.upsert(
        title=final_title,
        content=content,
        source_url=source_url,
        category=category,
        source_type="upload",
    )
    return KnowledgeItem.model_validate(row)



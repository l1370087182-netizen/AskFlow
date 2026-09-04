"""向量化触发接口：POST /api/embedding/run、POST /api/embedding/retry

手动触发入库流水线（阶段 5）：扫 status=0 → 切块 → 向量化 → 写 Milvus → 回写状态。
retry：把失败行（status=2，原因在 knowledge.vector_error）重置回待处理再跑流水线。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database.session import get_db
from model.KnowledgeModel import KnowledgeModel
from milvus.ingestion.pipeline import IngestionPipeline

router = APIRouter(prefix="/api/embedding", tags=["向量化"])


@router.post("/run")
def run_ingestion(
    limit: int | None = Query(
        default=None, ge=1, description="只处理前 N 条待向量化知识，不传则全部"
    ),
    db: Session = Depends(get_db),
):
    """触发向量化流水线，返回统计 {"total","success","failed","chunks"}

    注意：用同步 def（不是 async），FastAPI 会自动放到线程池执行——
    流水线内部是阻塞式 IO（HTTP 调 embedding、写 Milvus），不能堵住事件循环。
    """
    pipeline = IngestionPipeline(db)
    return pipeline.run(limit=limit)


@router.post("/retry")
def retry_failed(
    knowledge_id: int | None = Query(
        default=None, description="只重试这一条；不传则重试全部失败行"
    ),
    db: Session = Depends(get_db),
):
    """重试向量化失败的条目：status=2 → 0（清 vector_error）→ 立即跑流水线

    失败原因已在 knowledge.vector_error，前端先展示原因再引导重试。
    返回重试条数 + 本轮统计（结构与 /run 一致，无失败行时 total=0）。
    """
    q = db.query(KnowledgeModel).filter(
        KnowledgeModel.status == KnowledgeModel.STATUS_FAILED
    )
    if knowledge_id is not None:
        q = q.filter(KnowledgeModel.id == knowledge_id)
    rows = q.all()
    for row in rows:
        row.status = KnowledgeModel.STATUS_PENDING
        row.vector_error = None
    db.commit()

    pipeline = IngestionPipeline(db)
    stats = pipeline.run()
    return {"retried": len(rows), **stats}

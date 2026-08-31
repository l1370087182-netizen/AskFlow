"""向量化触发接口：POST /api/embedding/run

手动触发入库流水线（阶段 5）：扫 status=0 → 切块 → 向量化 → 写 Milvus → 回写状态。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database.session import get_db
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

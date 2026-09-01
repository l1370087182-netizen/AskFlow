"""检索调试接口：POST /api/retrieval/search（阶段 6）

走完整混合检索链路：BM25 + 向量 → RRF 融合 → Rerank 精排。
阶段 7 的对话接口内部也用同一条链路，这里先提供独立调试入口。

个人知识库：调试口同样按请求者隔离——挂 get_current_user，
search 传 uid=user.id，防止借调试接口窥探他人个人知识。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from database.session import get_db
from milvus.retrieval.hybird import HybridRetriever
from model.UserModel import UserModel

router = APIRouter(prefix="/api/retrieval", tags=["检索"])


class RetrievalRequest(BaseModel):
    """检索请求"""

    query: str = Field(..., min_length=1, description="检索问题")
    top_k: int = Field(default=5, ge=1, le=20, description="最终返回条数")
    category: str | None = Field(default=None, description="按技术分类过滤，可选")


@router.post("/search")
def search(
    body: RetrievalRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """混合检索，返回精排后的知识块（含各路分数与命中来源，便于调试）

    同步 def：内部是阻塞式 IO（BM25 构建、embedding、Milvus、rerank API），
    交给线程池执行，不堵事件循环。检索范围=全局块+本人个人块。
    """
    retriever = HybridRetriever(db)
    results = retriever.search(
        query=body.query,
        top_k=body.top_k,
        category=body.category,
        uid=user.id,
    )
    return {"query": body.query, "total": len(results), "results": results}

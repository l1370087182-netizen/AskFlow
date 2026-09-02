"""翻译接口：知识按需翻译（微软 Edge 免密钥接口，细节见 service/translate_service.py）。

按需翻译不写回知识库：结果只进 Redis 缓存（键带内容指纹，内容变更自动失效）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from database.session import get_db
from model.UserModel import UserModel
from schema.knowledge import KnowledgeTranslateOut
from service import translate_service

router = APIRouter(prefix="/api/translate", tags=["翻译"])


@router.post("/knowledge/{knowledge_id}", response_model=KnowledgeTranslateOut)
def translate_knowledge(
    knowledge_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """把指定知识整篇翻成中文（标题+正文，代码块保留）。

    归属同详情读路：他人个人条目 404 隐藏存在性。
    长文走切块并行翻译，可能需要数秒到十几秒；译文缓存 7 天。
    """
    try:
        out = translate_service.translate_knowledge(db, user.id, knowledge_id)
    except translate_service.TranslateError as e:
        raise HTTPException(status_code=502, detail=f"翻译服务暂不可用：{e}") from e
    if out is None:
        raise HTTPException(status_code=404, detail="未找到该知识")
    return KnowledgeTranslateOut(**out)

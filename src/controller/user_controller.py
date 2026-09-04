"""用户私有配置接口：自定义模型（api_key / base_url / model）按用户存取。

替代原先「前端 localStorage 明文存 + 随请求透传」的方案：
配置落 user 表（api_key Fernet 加密），读接口只回脱敏值。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from DAO.user_dao import UserDAO
from auth.deps import get_current_user
from database.session import get_db
from model.UserModel import UserModel
from schema.auth import LLMConfigOut, LLMConfigUpdate

router = APIRouter(prefix="/api/user", tags=["用户配置"])


def _mask_key(key: str) -> str:
    """脱敏展示：前 4 后 4，中间打码；太短只留首字符"""
    if not key:
        return ""
    if len(key) <= 8:
        return key[0] + "***"
    return f"{key[:4]}***{key[-4:]}"


def _config_out(dao: UserDAO, user_id: int) -> LLMConfigOut:
    cfg = dao.get_llm_config(user_id)
    has_custom = bool(cfg["base_url"].strip() and cfg["api_key"].strip())
    return LLMConfigOut(
        provider=cfg["provider"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        api_key_masked=_mask_key(cfg["api_key"]),
        has_custom=has_custom,
    )


@router.get("/llm", response_model=LLMConfigOut)
def get_llm_config(
    user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)
):
    """读取当前用户的模型配置（api_key 只回脱敏值，绝不回明文）"""
    return _config_out(UserDAO(db), user.id)


@router.put("/llm", response_model=LLMConfigOut)
def update_llm_config(
    body: LLMConfigUpdate,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新模型配置。规则：
    - base_url 空 → 清空整套配置（服务端无默认模型，清空后相关功能暂停，
      页面会再次提示配置）
    - base_url 非空：模型名必填（服务端不兜底）；
      api_key 空 → 保留原密钥（「留空保持不变」）；原无密钥又留空 → 400
    """
    dao = UserDAO(db)
    base_url = body.base_url.strip()

    if not base_url:
        dao.save_llm_config(
            user.id, provider="auto", base_url="", api_key="", model=""
        )
        return _config_out(dao, user.id)

    model = body.model.strip()
    if not model:
        raise HTTPException(
            status_code=400,
            detail="请填写模型名（model）——服务端不提供默认模型",
        )

    api_key = body.api_key.strip()
    if not api_key:
        # 留空 = 保留原密钥；原本就没有则报错
        api_key = dao.get_llm_config(user.id)["api_key"]
        if not api_key:
            raise HTTPException(
                status_code=400, detail="填了 API 地址就要填 API Key"
            )

    dao.save_llm_config(
        user.id,
        provider=body.provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    return _config_out(dao, user.id)

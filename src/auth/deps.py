"""鉴权依赖：从 Authorization: Bearer 头解析当前用户。

用法：端点签名加 `user: UserModel = Depends(get_current_user)`。
main.py 里也会以 `dependencies=[Depends(get_current_user)]` 挂到 router 级；
同一个函数对象在同一次请求内被 FastAPI 缓存，只鉴权一次、只查一次库。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from DAO.user_dao import UserDAO
from auth.security import TokenError, decode_access_token
from database.session import get_db
from model.UserModel import UserModel

# auto_error=False：缺 Authorization 头时返回 None 而不是 403，
# 由下面统一抛 401（前端按 401 统一跳登录页）
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> UserModel:
    """解析 Bearer token → 当前用户；任何异常态统一 401"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

    user = UserDAO(db).get_by_id(user_id)
    # token_ver 不匹配 = 签发后改过密码，旧 token 作废
    if user is None or user.token_ver != payload.get("ver", -1):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user

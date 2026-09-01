"""用户鉴权接口（阶段 11）：注册（邮箱验证码）/ 登录 / 忘记密码 / 当前用户。

本 router 是唯一不挂全局鉴权的 router（注册/登录本身还不能有 token）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from DAO.user_dao import UserDAO
from auth.deps import get_current_user
from auth.mailer import send_code, verify_code
from auth.security import create_access_token, hash_password, verify_password
from database.session import get_db
from model.UserModel import UserModel
from schema.auth import (
    LoginRequest,
    RegisterRequest,
    ResetRequest,
    SendCodeRequest,
    TokenResponse,
    UserOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["用户鉴权"])


def _user_out(user: UserModel) -> UserOut:
    return UserOut(id=user.id, email=user.email, nickname=user.nickname)


@router.post("/send-code")
def auth_send_code(body: SendCodeRequest, db: Session = Depends(get_db)):
    """发送邮箱验证码。注册前查重、重置前查存在，减少无效发送"""
    dao = UserDAO(db)
    exists = dao.get_by_email(body.email) is not None
    if body.purpose == "register" and exists:
        raise HTTPException(status_code=400, detail="该邮箱已注册，请直接登录")
    if body.purpose == "reset" and not exists:
        raise HTTPException(status_code=400, detail="该邮箱未注册")

    ok, message = send_code(body.email, body.purpose)
    if not ok:
        raise HTTPException(status_code=429, detail=message)
    return {"ok": True, "message": message}


@router.post("/register", response_model=TokenResponse)
def auth_register(body: RegisterRequest, db: Session = Depends(get_db)):
    """注册：验证码原子消费 + 建号，成功直接返回登录 token"""
    if not verify_code(body.email, "register", body.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    dao = UserDAO(db)
    if dao.get_by_email(body.email) is not None:
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    try:
        user = dao.create(
            email=body.email,
            password_hash=hash_password(body.password),
            nickname=body.nickname or body.email.split("@")[0],
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    token = create_access_token(user.id, user.token_ver)
    return TokenResponse(token=token, user=_user_out(user))


@router.post("/login", response_model=TokenResponse)
def auth_login(body: LoginRequest, db: Session = Depends(get_db)):
    """登录：统一话术，不区分「邮箱不存在」与「密码错误」（防枚举）"""
    user = UserDAO(db).get_by_email(body.email)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    token = create_access_token(user.id, user.token_ver)
    return TokenResponse(token=token, user=_user_out(user))


@router.post("/reset")
def auth_reset(body: ResetRequest, db: Session = Depends(get_db)):
    """忘记密码：验证码 + 新密码；成功后旧 token 全部失效（token_ver+1）"""
    if not verify_code(body.email, "reset", body.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    dao = UserDAO(db)
    user = dao.get_by_email(body.email)
    if user is None:
        raise HTTPException(status_code=400, detail="该邮箱未注册")

    dao.update_password(user.id, hash_password(body.new_password))
    return {"ok": True, "message": "密码已重置，请使用新密码登录"}


@router.get("/me", response_model=UserOut)
def auth_me(user: UserModel = Depends(get_current_user)):
    """当前用户信息（前端 topbar 展示 + token 探活）"""
    return _user_out(user)

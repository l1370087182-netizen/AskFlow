"""用户鉴权相关 DTO（注册/登录/忘记密码/验证码）。

邮箱用自写正则校验（不引 email-validator 依赖）。
"""
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 常用邮箱格式：本地部分 + 域名（不做 RFC 全量校验，够用且零依赖）
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _check_email(v: str) -> str:
    v = v.strip().lower()
    if len(v) > 255 or not _EMAIL_RE.match(v):
        raise ValueError("邮箱格式不正确")
    return v


class SendCodeRequest(BaseModel):
    """发送验证码（注册/重置密码两种用途）"""

    email: str = Field(..., description="目标邮箱")
    purpose: Literal["register", "reset"] = Field(..., description="用途")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _check_email(v)


class RegisterRequest(BaseModel):
    """注册：邮箱 + 验证码 + 密码"""

    email: str
    code: str = Field(..., min_length=6, max_length=6, description="6 位邮箱验证码")
    password: str = Field(..., min_length=6, max_length=64, description="密码，至少 6 位")
    nickname: str = Field(default="", max_length=64)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _check_email(v)


class LoginRequest(BaseModel):
    """登录：邮箱或管理员账号（.env 的 ADMIN_USERNAME）+ 密码。

    管理员账号不是邮箱，所以这里不做邮箱格式校验，只做长度限制；
    注册/重置密码仍然严格要求邮箱格式。
    """

    email: str = Field(..., min_length=1, max_length=255, description="邮箱或管理员账号")
    password: str = Field(..., min_length=1, max_length=64)


class ResetRequest(BaseModel):
    """忘记密码：验证码 + 新密码"""

    email: str
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6, max_length=64, description="新密码，至少 6 位")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _check_email(v)


class UserOut(BaseModel):
    """用户基本信息（对外不暴露任何敏感字段）"""

    id: int
    email: str
    nickname: str


class TokenResponse(BaseModel):
    token: str
    user: UserOut
    role: str = "user"  # user=普通用户 / admin=管理员（前端据此跳管理后台）


class AdminUserOut(BaseModel):
    """管理员后台的用户行：邮箱 + 注册时间 + 最近登录时间"""

    id: int
    email: str
    nickname: str
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    model_config = {"from_attributes": True}


class AdminUsersResponse(BaseModel):
    total: int
    users: list[AdminUserOut]


class LLMConfigOut(BaseModel):
    """用户私有模型配置（api_key 只回脱敏值）"""

    provider: str = "auto"
    base_url: str = ""
    model: str = ""
    api_key_masked: str = ""
    has_custom: bool = False


class LLMConfigUpdate(BaseModel):
    """更新用户私有模型配置

    - base_url 空 = 清空整套配置，回服务端默认模型
    - base_url 非空且 api_key 空 = 保留原密钥（前端提示「留空保持不变」）
    """

    provider: Literal["auto", "openai", "anthropic"] = "auto"
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)

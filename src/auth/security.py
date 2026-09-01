"""安全原语：密码哈希（标准库 pbkdf2）+ JWT（PyJWT）+ api_key 加解密（Fernet）。

设计要点：
- 密码：pbkdf2_hmac('sha256') 加盐 20 万轮，`hmac.compare_digest` 恒定时间比较
- JWT：HS256，payload 带 sub/exp/iat/ver；ver 与 user.token_ver 比对，
  改密后旧 token 立即失效
- api_key：Fernet 对称加密，密钥默认由 AUTH_SECRET_KEY 派生（SHA256→urlsafe_b64）；
  解密失败（如更换过密钥）返回空串并打 warning，视为「未配置自定义模型」，
  绝不让对话接口因此 500
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken

from core.config import settings

logger = logging.getLogger(__name__)

_ALGO = "HS256"


class TokenError(Exception):
    """token 缺失/过期/被篡改"""


# ---------- 密码 ----------

def hash_password(password: str) -> str:
    """pbkdf2_sha256$迭代次数$盐hex$密钥hex"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        settings.AUTH_PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${settings.AUTH_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码；格式损坏/参数异常一律视为校验失败（不抛异常）"""
    try:
        _scheme, iters_s, salt, dk_hex = stored.split("$")
        iters = int(iters_s)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iters
    )
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---------- JWT ----------

def create_access_token(user_id: int, token_ver: int, role: str = "user") -> str:
    """签发 JWT；role=user 普通用户 / role=admin 管理员（凭证在 .env，不进数据库）"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "ver": token_ver,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(minutes=settings.AUTH_TOKEN_TTL_MIN),
    }
    return jwt.encode(payload, settings.AUTH_SECRET_KEY, algorithm=_ALGO)


def decode_access_token(token: str) -> dict:
    """解出 payload；过期/篡改/格式错误统一抛 TokenError"""
    try:
        return jwt.decode(token, settings.AUTH_SECRET_KEY, algorithms=[_ALGO])
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e


# ---------- api_key 加解密 ----------

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """密钥优先 FERNET_KEY，留空则由 AUTH_SECRET_KEY 派生（SHA256→urlsafe_b64）"""
    global _fernet
    if _fernet is None:
        raw = settings.FERNET_KEY or base64.urlsafe_b64encode(
            hashlib.sha256(settings.AUTH_SECRET_KEY.encode("utf-8")).digest()
        ).decode("ascii")
        _fernet = Fernet(raw.encode("ascii"))
    return _fernet


def encrypt_secret(plain: str) -> str:
    """空串进空串出（空=未配置自定义模型）"""
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """解密失败（密文损坏/换过密钥）返回空串并 warning，不抛异常"""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as e:
        logger.warning("[auth] api_key 解密失败（视为未配置自定义模型）：%s", e)
        return ""

"""邮箱验证码：生成/存取（Redis）+ 发送（SMTP，未配置时打印到日志）。

Redis 键设计（连接参数与爬虫/卡片模块同一套）：
    auth:code:{purpose}:{email}     验证码本体        TTL CODE_TTL_SEC
    auth:code:cd:{purpose}:{email}  重发冷却标记       SET NX，TTL CODE_RESEND_COOLDOWN
    auth:code:cnt:{email}           单邮箱日发送计数    TTL 24h，上限 CODE_DAILY_LIMIT
    auth:code:att:{purpose}:{email} 校验失败次数       达 CODE_MAX_ATTEMPTS 后验证码作废
"""
from __future__ import annotations

import logging
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

import redis

from core.config import settings
from util.redis_util import make_redis

logger = logging.getLogger(__name__)

PURPOSES = ("register", "reset")
_PURPOSE_ZH = {"register": "注册", "reset": "重置密码"}


def _redis() -> redis.Redis:
    """Redis 客户端（统一工厂：短超时+失败即抛，见 util/redis_util）"""
    return make_redis()


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def _send_mail(to: str, subject: str, body: str) -> None:
    """SMTP 未配置时降级为打印到控制台（本地开发兜底），配置异常直接抛出由上层提示"""
    if not _smtp_configured():
        # 用 print 而非 logger：uvicorn 默认不输出应用层 INFO 日志，
        # 打印到 stdout 才能保证开发时一定看得到验证码
        logger.info("[mailer] SMTP 未配置，验证码走控制台打印 → %s", to)
        print(f"[验证码] 收件人 {to}，邮件正文：\n{body}", flush=True)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = to
    msg.set_content(body)

    if settings.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT,
                              context=ssl.create_default_context(), timeout=15) as s:
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)


def send_code(email: str, purpose: Literal["register", "reset"]) -> tuple[bool, str]:
    """生成并发送验证码；返回 (是否成功, 提示语)"""
    r = _redis()
    zh = _PURPOSE_ZH[purpose]

    # 1) 重发冷却：SET NX 抢锁，抢不到说明刚发过
    cd_key = f"auth:code:cd:{purpose}:{email}"
    if not r.set(cd_key, "1", nx=True, ex=settings.CODE_RESEND_COOLDOWN):
        return False, f"发送太频繁，请 {settings.CODE_RESEND_COOLDOWN} 秒后再试"

    # 2) 单邮箱日上限
    cnt_key = f"auth:code:cnt:{email}"
    cnt = r.incr(cnt_key)
    if cnt == 1:
        r.expire(cnt_key, 24 * 3600)
    if cnt > settings.CODE_DAILY_LIMIT:
        return False, "今日验证码发送次数已达上限，请明天再试"

    # 3) 生成 6 位数字码（安全随机），存 Redis；旧的试错计数一并清零
    code = f"{secrets.randbelow(1_000_000):06d}"
    r.set(f"auth:code:{purpose}:{email}", code, ex=settings.CODE_TTL_SEC)
    r.delete(f"auth:code:att:{purpose}:{email}")

    minutes = settings.CODE_TTL_SEC // 60
    body = (
        f"你正在{_PURPOSE_ZH[purpose]}智能技术学习系统。\n"
        f"验证码：{code}\n"
        f"{minutes} 分钟内有效，请勿泄露给他人。"
    )
    try:
        _send_mail(email, f"【技术学习系统】{zh}验证码", body)
    except Exception as e:  # noqa: BLE001 —— 发送失败给出明确提示，不泄露细节
        logger.exception("[mailer] 验证码邮件发送失败")
        return False, f"验证码邮件发送失败，请稍后重试（{e.__class__.__name__}）"

    if not _smtp_configured():
        return True, "验证码已发送（SMTP 未配置，码已打印到后端日志）"
    return True, "验证码已发送，请注意查收邮件"


def verify_code(email: str, purpose: str, code: str) -> bool:
    """校验验证码：成功原子消费（GETDEL，防双花）；失败累计次数，超限作废"""
    r = _redis()
    code_key = f"auth:code:{purpose}:{email}"
    att_key = f"auth:code:att:{purpose}:{email}"

    stored = r.get(code_key)
    if not stored:
        return False

    # 试错次数达上限 → 作废验证码，必须重新发送
    if int(r.get(att_key) or 0) >= settings.CODE_MAX_ATTEMPTS:
        r.delete(code_key)
        return False

    if secrets.compare_digest(stored, code.strip()):
        r.getdel(code_key)
        r.delete(att_key)
        return True

    r.incr(att_key)
    r.expire(att_key, settings.CODE_TTL_SEC)
    return False

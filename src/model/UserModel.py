"""user 表：用户账号 + 私有模型配置（阶段 11：用户鉴权）。

- 密码哈希格式：pbkdf2_sha256$迭代次数$盐(hex)$派生密钥(hex)
- token_ver：改密后 +1，令改密前签发的 JWT 立即失效
- llm_*：用户私有模型配置（api_key 以 Fernet 密文存储），空=用服务端默认模型
"""
from database.session import Base
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class UserModel(Base):
    """用户账号"""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    email: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True, comment="登录邮箱"
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="密码哈希（pbkdf2_sha256）"
    )
    nickname: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="昵称（可选）"
    )
    token_ver: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="token 版本号，改密后 +1 使旧 token 失效"
    )

    # ---- 用户私有模型配置（一对一直接放 user 表，省 join）----
    llm_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto",
        comment="模型协议：auto / openai / anthropic",
    )
    llm_base_url: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="模型 API 地址"
    )
    llm_api_key_enc: Mapped[str] = mapped_column(
        String(512), nullable=False, default="",
        comment="api_key 的 Fernet 密文；空=用服务端默认模型",
    )
    llm_model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="模型名"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now,
        comment="更新时间",
    )

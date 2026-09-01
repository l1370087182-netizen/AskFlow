"""user 表的 DAO：账号增查、改密、私有模型配置读写。"""
from datetime import datetime

from sqlalchemy.orm import Session

from auth.security import decrypt_secret, encrypt_secret
from model.UserModel import UserModel


class UserDAO:
    """用户 DAO（构造注入 db，写方法内部 commit，与现有 DAO 风格一致）"""

    def __init__(self, db: Session):
        self.db = db

    # ---------- 账号 ----------

    def create(
        self, *, email: str, password_hash: str, nickname: str = ""
    ) -> UserModel:
        row = UserModel(email=email, password_hash=password_hash, nickname=nickname)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_by_id(self, user_id: int) -> UserModel | None:
        return self.db.query(UserModel).filter(UserModel.id == user_id).first()

    def get_by_email(self, email: str) -> UserModel | None:
        return (
            self.db.query(UserModel)
            .filter(UserModel.email == email.strip().lower())
            .first()
        )

    def update_password(self, user_id: int, password_hash: str) -> None:
        """改密同时 token_ver+1：改密前签发的 JWT 全部作废"""
        row = self.get_by_id(user_id)
        if not row:
            return
        row.password_hash = password_hash
        row.token_ver = (row.token_ver or 0) + 1
        self.db.commit()

    def touch_last_login(self, user_id: int) -> None:
        """刷新最近登录时间（登录成功时调用；注册即首次登录也写）"""
        row = self.get_by_id(user_id)
        if not row:
            return
        row.last_login_at = datetime.now()
        self.db.commit()

    def list_all(self) -> list[UserModel]:
        """全部注册用户，按 id 升序（管理员后台用）"""
        return self.db.query(UserModel).order_by(UserModel.id).all()

    # ---------- 私有模型配置 ----------

    def get_llm_config(self, user_id: int) -> dict:
        """读配置，api_key 解密后返回（内部使用，勿直接回传前端明文）"""
        row = self.get_by_id(user_id)
        if not row:
            return {"provider": "auto", "base_url": "", "api_key": "", "model": ""}
        return {
            "provider": row.llm_provider or "auto",
            "base_url": row.llm_base_url or "",
            "api_key": decrypt_secret(row.llm_api_key_enc),
            "model": row.llm_model or "",
        }

    def save_llm_config(
        self,
        user_id: int,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
    ) -> None:
        """存配置，api_key 加密落库"""
        row = self.get_by_id(user_id)
        if not row:
            return
        row.llm_provider = provider
        row.llm_base_url = base_url
        row.llm_api_key_enc = encrypt_secret(api_key)
        row.llm_model = model
        self.db.commit()

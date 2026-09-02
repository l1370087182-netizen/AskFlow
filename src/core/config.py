from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file = ".env",
        env_file_encoding = "utf-8",
    )
    EMBEDDING_MODEL: str = Field(... ,description="Embedding模型")
    EMBEDDING_KEY: str = Field(... ,description="Embedding密钥")
    EMBEDDING_BASE_URL: str = Field(default="", description="Embedding API地址（OpenAI兼容，可带或不带 /embeddings 后缀）")

    MYSQL_HOST: str = Field(... ,description="数据库主机")
    MYSQL_PORT: int = Field(... ,description="数据库端口")
    MYSQL_USER: str = Field(... ,description="数据库用户")
    MYSQL_PASSWORD: str = Field(... ,description="数据库密码")
    MYSQL_DATABASE: str = Field(... ,description="数据库名称")
    MYSQL_CHARSET: str = Field(... ,description="数据库字符集")
    MYSQL_POOL_SIZE: int = Field(... ,description="数据库连接池大小")
    MYSQL_POOL_MAX_OVERFLOW: int = Field(... ,description="数据库连接池最大溢出数")

        # ---------- 本地目录 ----------
    DATA_DIR: str = Field(default="./data", description="数据目录（上传文件、BM25缓存）")
    SESSION_DIR: str = Field(default="./sessions", description="会话目录（对话历史）")

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            f"?charset={self.MYSQL_CHARSET}"
        )

        # ---------- Redis ----------
    REDIS_HOST: str = Field(default="127.0.0.1", description="Redis主机")
    REDIS_PORT: int = Field(default=6379, description="Redis端口")
    REDIS_DB: int = Field(default=0, description="Redis DB编号")
    REDIS_PASSWORD: str = Field(default="", description="Redis密码，无则空")

        # ---------- Milvus ----------
    MILVUS_HOST: str = Field(default="127.0.0.1", description="Milvus主机")
    MILVUS_PORT: int = Field(default=19530, description="Milvus端口")
    MILVUS_LITE_PATH: str = Field(default="", description="非空则用嵌入式 Milvus Lite（本地文件路径），忽略 HOST/PORT；低内存服务器部署用。注意不能叫 MILVUS_URI，那是 pymilvus 保留环境变量")

        # ---------- Rerank ----------
    RERANK_MODEL: str = Field(default="BAAI/bge-reranker-v2-m3", description="重排序模型")
    RERANK_BASE_URL: str = Field(default="", description="重排序API地址，留空则复用 EMBEDDING_BASE_URL 同域")

        # ---------- 编排检索 ----------
    RETRIEVAL_ORCHESTRATOR: bool = Field(
        default=True,
        description="讲解模式编排检索开关：多查询改写+跨变体 RRF 融合；关闭则回退单次查询检索",
    )

        # ---------- Chat ----------
    CHAT_MODEL: str = Field(default="", description="对话大模型")
    CHAT_KEY: str = Field(default="", description="对话大模型密钥")
    CHAT_BASE_URL: str = Field(default="", description="对话大模型API地址")
    CHAT_PROVIDER: str = Field(default="auto", description="对话协议：openai / anthropic / auto（按地址识别）")

        # ---------- OCR（视觉识别，默认复用 Chat/Embedding 的地址与密钥）----------
    OCR_MODEL: str = Field(default="Qwen/Qwen3-VL-32B-Instruct", description="视觉OCR模型")
    OCR_BASE_URL: str = Field(default="", description="OCR API地址，留空复用 CHAT_BASE_URL")
    OCR_KEY: str = Field(default="", description="OCR密钥，留空复用 CHAT_KEY")

        # ---------- 用户鉴权 ----------
    AUTH_SECRET_KEY: str = Field(
        ...,
        description="JWT 签名密钥，必须配置（它同时是 api_key 加密密钥的派生源，"
        "更换会导致存量密文解不出）。生成：python -c \"import secrets;print(secrets.token_urlsafe(48))\"",
    )
    AUTH_TOKEN_TTL_MIN: int = Field(default=1440, description="登录 token 有效期（分钟），默认 24h")
    AUTH_PBKDF2_ITERATIONS: int = Field(default=200_000, description="密码哈希迭代次数")
    FERNET_KEY: str = Field(default="", description="api_key 加密密钥，留空则由 AUTH_SECRET_KEY 派生")

        # ---------- 管理员（固定凭证，只在这里改，不进数据库） ----------
    ADMIN_USERNAME: str = Field(default="adminljj", description="管理员账号；在登录页直接输入即可登录")
    ADMIN_PASSWORD: str = Field(default="", description="管理员密码；留空=禁用管理员登录")

        # ---------- 验证码邮件（SMTP 未配置时验证码只打印到后端日志，便于本地开发）----------
    SMTP_HOST: str = Field(default="", description="SMTP 服务器，如 smtp.qq.com")
    SMTP_PORT: int = Field(default=465, description="SMTP 端口，SSL 一般 465")
    SMTP_USE_SSL: bool = Field(default=True, description="是否用 SSL（465）；False 走 STARTTLS（587）")
    SMTP_USER: str = Field(default="", description="SMTP 登录账号（一般即发件邮箱）")
    SMTP_PASSWORD: str = Field(default="", description="SMTP 授权码（不是邮箱登录密码）")
    SMTP_FROM: str = Field(default="", description="发件人，留空用 SMTP_USER")

        # ---------- 验证码策略 ----------
    CODE_TTL_SEC: int = Field(default=300, description="验证码有效期（秒）")
    CODE_RESEND_COOLDOWN: int = Field(default=60, description="同邮箱同用途重发冷却（秒）")
    CODE_DAILY_LIMIT: int = Field(default=10, description="单邮箱每日验证码上限")
    CODE_MAX_ATTEMPTS: int = Field(default=5, description="单个验证码允许的校验失败次数")


settings = Settings()


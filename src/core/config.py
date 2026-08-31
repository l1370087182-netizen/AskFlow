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
    MILVUS_URI: str = Field(default="", description="非空则用嵌入式 Milvus Lite（本地文件路径），忽略 HOST/PORT；低内存服务器部署用")

        # ---------- Rerank ----------
    RERANK_MODEL: str = Field(default="BAAI/bge-reranker-v2-m3", description="重排序模型")
    RERANK_BASE_URL: str = Field(default="", description="重排序API地址，留空则复用 EMBEDDING_BASE_URL 同域")

        # ---------- Chat ----------
    CHAT_MODEL: str = Field(default="", description="对话大模型")
    CHAT_KEY: str = Field(default="", description="对话大模型密钥")
    CHAT_BASE_URL: str = Field(default="", description="对话大模型API地址")
    CHAT_PROVIDER: str = Field(default="auto", description="对话协议：openai / anthropic / auto（按地址识别）")

        # ---------- OCR（视觉识别，默认复用 Chat/Embedding 的地址与密钥）----------
    OCR_MODEL: str = Field(default="Qwen/Qwen3-VL-32B-Instruct", description="视觉OCR模型")
    OCR_BASE_URL: str = Field(default="", description="OCR API地址，留空复用 CHAT_BASE_URL")
    OCR_KEY: str = Field(default="", description="OCR密钥，留空复用 CHAT_KEY")


settings = Settings()


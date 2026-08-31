from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.MYSQL_POOL_SIZE,
    max_overflow=settings.MYSQL_POOL_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=60,
)

# 创建会话
SessionLocal = sessionmaker(
    autocommit=False, 
    autoflush=False, 
    bind=engine
)

class Base(DeclarativeBase):
    """所有 ORM Model 的基类"""
    pass

# 获取数据库会话
def get_db():
    """
    FastAPI 依赖注入用：
    yield 会话，请求结束自动关闭
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine)

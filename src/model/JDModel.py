"""jd 表：一次 JD 分析请求的原始数据与结果。"""
from database.session import Base
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime


class JDModel(Base):
    """JD 截图分析记录：图片落盘路径 + OCR 文本 + 分析结果"""

    __tablename__ = "jd"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键"
    )
    # 所属用户；存量行留 NULL（阶段 11 之前的旧数据作废，任何用户都查不到）
    user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="所属用户"
    )
    filename: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="原始文件名"
    )
    image_path: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="截图落盘路径（storage/jd/）"
    )
    ocr_text: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="",
        comment="OCR 识别出的 JD 文本",
    )
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="LLM 提炼的职位名称"
    )
    summary: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", comment="职位一句话概括"
    )
    analysis_raw: Mapped[str] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"),
        nullable=False,
        default="",
        comment="LLM 分析返回的原始 JSON",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
        comment="更新时间",
    )

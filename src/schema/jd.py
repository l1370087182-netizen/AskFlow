from datetime import datetime

from pydantic import BaseModel, Field


class TechStackItem(BaseModel):
    """单条技术栈"""

    name: str
    category: str
    level: str = Field(description="required=必需，bonus=加分项")
    note: str = ""

    model_config = {"from_attributes": True}


class JDAnalyzeResponse(BaseModel):
    """JD 分析完整结果"""

    jd_id: int
    title: str
    summary: str
    ocr_text: str
    tech_stack: list[TechStackItem]
    soft_requirements: list[str]


class JDItem(BaseModel):
    """列表里的 JD 概要"""

    id: int
    filename: str
    title: str
    summary: str
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class JDListResponse(BaseModel):
    total: int
    items: list[JDItem]

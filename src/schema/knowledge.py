from pydantic import BaseModel, Field
from datetime import datetime


class KnowledgeItem(BaseModel):
    """单条知识的响应模型"""
    id: int
    title: str
    source_url: str
    category: str
    source_type: str
    status: int = Field(..., description="0=待向量化，1=已向量化，2=向量化失败")
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {
        "from_attributes": True,
    }

class KnowledgeListResponse(BaseModel):
    """知识列表的响应模型"""

    total: int
    items: list[KnowledgeItem]

class KnowledgeCreateRequest(BaseModel):
    """手动创建知识的请求模型"""

    title: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1)
    source_url: str | None = Field(default="", max_length=768)
    category: str = Field(default="general", max_length=128)
    source_type: str = Field(default="upload", max_length=32)


class KnowledgeDetailResponse(KnowledgeItem):
    """知识详情：列表项 + 正文"""

    content: str



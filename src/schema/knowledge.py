from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal


class KnowledgeItem(BaseModel):
    """单条知识的响应模型"""
    id: int
    user_id: int = Field(default=0, description="所属用户；0=全局知识")
    title: str
    source_url: str
    category: str
    source_type: str
    status: int = Field(..., description="0=待向量化，1=已向量化，2=向量化失败")
    vector_error: str | None = Field(
        default=None, description="向量化失败原因摘要（status=2 时有值）"
    )
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
    """手动创建知识的请求模型（全局语料）"""

    title: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1)
    source_url: str | None = Field(default="", max_length=768)
    category: str = Field(default="general", max_length=128)
    source_type: str = Field(default="upload", max_length=32)


class KnowledgeDetailResponse(KnowledgeItem):
    """知识详情：列表项 + 正文"""

    content: str


# ---------- 个人知识库（我的知识） ----------

class KnowledgeMyCreateRequest(BaseModel):
    """手工添加个人知识：title 1..512；content 有效长度 ≥50 字（控制器校验，400）"""

    title: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1)
    category: str = Field(default="general", max_length=128)


class KnowledgeMyUpdateRequest(BaseModel):
    """编辑个人知识：至少传一项（控制器校验，全空 400）"""

    title: str | None = Field(default=None, min_length=1, max_length=512)
    content: str | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, max_length=128)


class KnowledgeCrawlRequest(BaseModel):
    """提交整站浅爬任务：种子 URL + 分类 + 页数（钳制 1..20，默认 10）"""

    url: str = Field(..., min_length=1, max_length=768)
    category: str = Field(default="general", max_length=128)
    max_pages: int = Field(default=10, ge=1, le=20)


class CrawlPageOut(BaseModel):
    """任务内单页结果"""

    url: str
    ok: bool
    cleaned: bool = False
    knowledge_id: int | None = None
    error: str = ""


class CrawlTaskOut(BaseModel):
    """爬取/联网检索任务进度响应（生命周期以 agent_task 为准，Redis 供实时明细）"""

    task_id: str
    uid: int
    url: str
    category: str
    max_pages: int
    status: str = Field(..., description="pending|running|searching|done|partial|failed|canceled")
    done_pages: int = 0
    failed_pages: int = 0
    skipped_pages: int = 0
    current_url: str = ""
    pages: list[CrawlPageOut] = []
    error: str = ""
    heartbeat: float = 0.0
    created_at: float = 0.0
    finished_at: float = 0.0
    # 联网检索（web_search 任务）扩展字段：
    phase: str = ""               # 检索阶段文案（生成检索词/搜索中/筛选网页）
    topic: str = ""               # 检索主题
    child_task_id: str = ""       # 交接出的子爬取任务（服务端按 parent_id 反查填充）


class CrawlActiveOut(BaseModel):
    """当前全部活跃爬取任务（并行化后单用户可多个）"""

    tasks: list[CrawlTaskOut] = []


# ---------- 知识翻译（Edge 免密钥接口，按需翻译不改库存） ----------

class KnowledgeTranslateOut(BaseModel):
    """知识翻译结果：详情弹窗按需展示，不写回知识库"""

    id: int
    detected: str | None = Field(default=None, description="自动检测的源语言")
    same_language: bool = Field(default=False, description="原文已是中文，原样返回")
    title: str
    content: str
    cached: bool = Field(default=False, description="是否命中翻译缓存")


# ---------- AI 添加（对话式定题 → 自动爬取） ----------

class AIAddMessage(BaseModel):
    """AI 添加对话的一条历史消息"""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=2000)


class AIAddRequest(BaseModel):
    """AI 添加请求：前端每轮带上完整对话历史（服务端不存状态）"""

    messages: list[AIAddMessage] = Field(..., min_length=1, max_length=20)


class AIAddProposal(BaseModel):
    """AI 给出的爬取建议（action=crawl 时）"""

    url: str
    title: str = ""
    category: str = "general"
    max_pages: int = 10


class AIAddResponse(BaseModel):
    """AI 添加响应。

    action=ask   —— AI 追问/确认纠错，对话继续（task_id 可能带回正在进行的任务）
    action=crawl —— AI 已提交爬取任务，task_id 必带，前端直接跳进度面板
    """

    action: Literal["ask", "crawl"]
    message: str
    proposal: AIAddProposal | None = None
    task_id: str | None = None

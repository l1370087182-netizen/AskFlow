from typing import Literal

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """用户自定义大模型接入配置（前端设置，随请求透传）

    base_url + api_key 都填了才生效；协议 auto 时按地址识别（含 anthropic 走
    Anthropic Messages，否则 OpenAI 兼容）。
    """

    provider: Literal["auto", "openai", "anthropic"] = "auto"
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class ChatRequest(BaseModel):
    """对话请求：统一入口，靠 mode 区分讲解/费曼两种模式"""

    session_id: str = Field(
        default="default",
        max_length=64,
        description="会话标识，同一会话共享记忆；两种模式各自独立上下文",
    )
    mode: Literal["ask", "teach"] = Field(
        default="ask",
        description="ask=讲解模式（AI当老师）；teach=费曼模式（AI当学生）",
    )
    message: str = Field(..., min_length=1, max_length=8000, description="本轮消息")
    topic: str | None = Field(
        default=None,
        description="费曼模式选题；不传则以首条消息作为主题",
    )
    finish: bool = Field(
        default=False,
        description="费曼模式：主动结束讲解，触发总结评分",
    )
    llm: LLMConfig | None = Field(
        default=None,
        description="用户自定义模型接入；不传或为空则用服务端默认模型",
    )


class PingRequest(BaseModel):
    """模型连通性测试请求"""

    llm: LLMConfig

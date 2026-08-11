from __future__ import annotations

from enum import Enum

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


class CopyStyle(str, Enum):
    FRIENDLY = "friendly"
    PROFESSIONAL = "professional"
    LIVELY = "lively"
    PREMIUM = "premium"
    MINIMAL = "minimal"
    STORY = "story"


class ContentScene(str, Enum):
    SHORT_VIDEO = "short_video"
    SOCIAL_POST = "social_post"
    PRODUCT_LAUNCH = "product_launch"
    PROMOTION = "promotion"
    LIVE_STREAM = "live_stream"
    PRODUCT_DETAIL = "product_detail"


STYLE_LABELS: dict[CopyStyle, str] = {
    CopyStyle.FRIENDLY: "亲切种草",
    CopyStyle.PROFESSIONAL: "专业可信",
    CopyStyle.LIVELY: "活泼有梗",
    CopyStyle.PREMIUM: "高级质感",
    CopyStyle.MINIMAL: "克制简洁",
    CopyStyle.STORY: "故事叙述",
}

SCENE_LABELS: dict[ContentScene, str] = {
    ContentScene.SHORT_VIDEO: "短视频发布",
    ContentScene.SOCIAL_POST: "社交媒体",
    ContentScene.PRODUCT_LAUNCH: "新品首发",
    ContentScene.PROMOTION: "促销活动",
    ContentScene.LIVE_STREAM: "直播预告",
    ContentScene.PRODUCT_DETAIL: "商品详情",
}

FESTIVAL_SUGGESTIONS = ("七夕", "中秋", "国庆", "双11", "双12", "元旦", "春节", "618")


def _trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class ProductSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: AnyHttpUrl | None = None
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        return _trimmed(value)

    @model_validator(mode="after")
    def require_endpoint_for_key(self) -> "ProductSearchConfig":
        if self.api_key and not self.endpoint_url:
            raise ValueError("填写商品搜索 API Key 时必须同时填写服务地址")
        if self.api_key and self.endpoint_url and self.endpoint_url.scheme != "https":
            raise ValueError("携带商品搜索 API Key 时服务地址必须使用 HTTPS")
        return self


class ProductReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_url: AnyHttpUrl
    search: ProductSearchConfig = Field(default_factory=ProductSearchConfig)


class ProductReference(BaseModel):
    source_url: str
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    attributes: dict[str, str] = Field(default_factory=dict)


class GenerateCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_brief: str = Field(min_length=2, max_length=2000)
    style: CopyStyle
    scene: ContentScene
    festival: str | None = Field(default=None, max_length=40)
    product_url: AnyHttpUrl | None = None
    product_search: ProductSearchConfig = Field(default_factory=ProductSearchConfig)

    @field_validator("content_brief")
    @classmethod
    def normalize_brief(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("内容要点至少需要 2 个字符")
        return normalized

    @field_validator("festival")
    @classmethod
    def normalize_festival(cls, value: str | None) -> str | None:
        return _trimmed(value)

    @model_validator(mode="after")
    def require_product_for_search_config(self) -> "GenerateCopyRequest":
        if not self.product_url and (
            self.product_search.endpoint_url or self.product_search.api_key
        ):
            raise ValueError("配置商品搜索服务前，请先填写商品链接")
        return self


class GeneratedCopyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=30)
    body: str = Field(min_length=10, max_length=1000)

    @field_validator("title", "body")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class GenerateCopyResponse(BaseModel):
    title: str
    body: str
    provider: str
    model: str
    product_reference: ProductReference | None = None

# -*- coding: utf-8 -*-
"""webapp.ai_copy.contracts 模块：AI 文案的 Pydantic 数据契约。

定义：
- CopyStyle / ContentScene: 文案风格与内容场景枚举
- STYLE_LABELS / SCENE_LABELS: 枚举值到中文标签的映射
- ProductSearchConfig: 专用商品搜索服务配置（可选）
- ProductReferencesRequest / ProductReference: 商品链接读取请求/响应
- SellingPointReference / SellingPointCatalogUploadResponse: 卖点条目与上传响应
- GenerateCopyRequest / GenerateCopyResponse / GeneratedCopyDraft: 文案生成请求/响应/草稿
"""
from __future__ import annotations

from enum import Enum

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


class CopyStyle(str, Enum):
    """文案风格枚举。"""
    OLD_MONEY_LUXURY = "old_money_luxury"      # 老钱轻奢
    RELAXED_NATURAL = "relaxed_natural"        # 自然松弛
    GENTLE_HEALING = "gentle_healing"          # 治愈温柔
    RETRO_ATMOSPHERE = "retro_atmosphere"      # 复古氛围
    ATMOSPHERIC_SEEDING = "atmospheric_seeding"  # 氛围感种草
    SWEET_CUTE = "sweet_cute"                  # 甜美可爱
    COOL_BOLD = "cool_bold"                    # 酷感飒爽


class ContentScene(str, Enum):
    """内容场景枚举。"""
    FITNESS = "fitness"                       # 运动健身
    PARENTING_AND_BABY = "parenting_and_baby"  # 母婴亲子
    LEISURE_TRAVEL = "leisure_travel"          # 度假休闲出游
    DAILY_STYLING = "daily_styling"           # 日常穿搭
    WORK_COMMUTE = "work_commute"             # 职场通勤
    ROMANTIC_DATE = "romantic_date"           # 浪漫约会
    SMART_CASUAL_GATHERING = "smart_casual_gathering"  # 轻正式聚会
    HOLIDAY_GIFTING = "holiday_gifting"       # 节日礼赠
    SELF_REWARD = "self_reward"              # 自用犒赏


class SellingPointInputMode(str, Enum):
    """商品核心卖点的输入方式。"""

    EXCEL = "excel"
    MANUAL = "manual"


# 风格枚举→中文标签
STYLE_LABELS: dict[CopyStyle, str] = {
    CopyStyle.OLD_MONEY_LUXURY: "老钱轻奢",
    CopyStyle.RELAXED_NATURAL: "自然松弛",
    CopyStyle.GENTLE_HEALING: "治愈温柔",
    CopyStyle.RETRO_ATMOSPHERE: "复古氛围",
    CopyStyle.ATMOSPHERIC_SEEDING: "氛围感种草",
    CopyStyle.SWEET_CUTE: "甜美可爱",
    CopyStyle.COOL_BOLD: "酷感飒爽",
}

# 场景枚举→中文标签
SCENE_LABELS: dict[ContentScene, str] = {
    ContentScene.FITNESS: "运动健身",
    ContentScene.PARENTING_AND_BABY: "母婴亲子",
    ContentScene.LEISURE_TRAVEL: "度假休闲出游",
    ContentScene.DAILY_STYLING: "日常穿搭",
    ContentScene.WORK_COMMUTE: "职场通勤",
    ContentScene.ROMANTIC_DATE: "浪漫约会",
    ContentScene.SMART_CASUAL_GATHERING: "轻正式聚会",
    ContentScene.HOLIDAY_GIFTING: "节日礼赠",
    ContentScene.SELF_REWARD: "自用犒赏",
}

# 节日氛围建议（前端下拉预设）
FESTIVAL_SUGGESTIONS = ("情人节", "女神节", "520", "暑假", "开学季", "圣诞节")


def _trimmed(value: str | None) -> str | None:
    """去首尾空白，空字符串转 None。"""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_product_urls(value: object, *, required: bool) -> list[str]:
    """归一化商品链接列表：去重保序，限制最多 20 个。

    :param required: True 时至少需要一个链接
    """
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ValueError("商品链接必须是列表")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        product_url = str(item).strip()
        if not product_url or product_url in seen:
            continue
        seen.add(product_url)
        normalized.append(product_url)
    if required and not normalized:
        raise ValueError("请至少填写一个商品链接")
    if len(normalized) > 20:
        raise ValueError("一次最多支持 20 个商品链接")
    return normalized


class ProductSearchConfig(BaseModel):
    """专用商品搜索服务配置（可选，用于公开商品页无法直接解析时）。"""

    model_config = ConfigDict(extra="forbid")

    endpoint_url: AnyHttpUrl | None = None    # 搜索服务地址（必须 HTTPS 才能携带 API Key）
    api_key: str | None = Field(default=None, max_length=4096)  # 搜索服务 API Key

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        """去首尾空白。"""
        return _trimmed(value)

    @model_validator(mode="after")
    def require_endpoint_for_key(self) -> "ProductSearchConfig":
        """携带 API Key 时必须同时填写服务地址且使用 HTTPS。"""
        if self.api_key and not self.endpoint_url:
            raise ValueError("填写商品搜索 API Key 时必须同时填写服务地址")
        if self.api_key and self.endpoint_url and self.endpoint_url.scheme != "https":
            raise ValueError("携带商品搜索 API Key 时服务地址必须使用 HTTPS")
        return self


class ProductReferencesRequest(BaseModel):
    """商品链接读取请求（用于 /product-references 接口）。"""

    model_config = ConfigDict(extra="forbid")

    product_urls: list[AnyHttpUrl] = Field(min_length=1, max_length=20)
    search: ProductSearchConfig = Field(default_factory=ProductSearchConfig)

    @field_validator("product_urls", mode="before")
    @classmethod
    def normalize_product_urls(cls, value: object) -> list[str]:
        """归一化商品链接列表（必填）。"""
        return _normalize_product_urls(value, required=True)


class ProductReference(BaseModel):
    """商品读取结果：标题、摘要、结构化属性。"""

    source_url: str
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    attributes: dict[str, str] = Field(default_factory=dict)


class SellingPointReference(BaseModel):
    """单条卖点条目：商品 ID/货号 + 核心内容卖点。"""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=100)
    selling_point: str = Field(min_length=1, max_length=2000)


class SellingPointCatalogUploadResponse(BaseModel):
    """卖点 Excel 上传响应。"""

    catalog_id: str          # 目录 ID（用于后续生成引用）
    filename: str            # 安全化后的文件名
    row_count: int           # 解析出的条目数
    entries: list[SellingPointReference]  # 全部条目


class GenerateCopyRequest(BaseModel):
    """文案生成请求。"""

    model_config = ConfigDict(extra="forbid")

    selling_point_input_mode: SellingPointInputMode = SellingPointInputMode.EXCEL
    selling_point_catalog_id: str | None = Field(default=None, max_length=64)  # Excel 卖点目录 ID
    product_identifiers: list[str] = Field(default_factory=list, max_length=20)  # 商品 ID/货号列表
    manual_selling_point: str | None = Field(default=None, max_length=2000)  # 直接输入的核心卖点
    style: CopyStyle | None = None            # 文案风格；与自定义风格二选一
    scene: ContentScene | None = None          # 内容场景；与自定义场景二选一
    festival: str | None = Field(default=None, max_length=40)  # 可选节日氛围
    custom_style: str | None = Field(default=None, max_length=100)  # 自定义文案风格，与预设互斥
    custom_scene: str | None = Field(default=None, max_length=100)  # 自定义内容场景，与预设互斥
    custom_festival: str | None = Field(default=None, max_length=80)  # 自定义节日氛围，与预设互斥
    product_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=20)  # 可选商品链接
    product_search: ProductSearchConfig = Field(default_factory=ProductSearchConfig)  # 可选搜索服务
    title_max_chars: int | None = Field(      # 标题目标字符数（字段名为兼容旧客户端保留）
        default=None,
        ge=2,
        le=100,
        description="生成标题的目标汉字数（每条结果必须正好等于目标值，标点数字不计）",
    )
    body_max_chars: int | None = Field(       # 正文目标字符数（字段名为兼容旧客户端保留）
        default=None,
        ge=10,
        le=1000,
        description="生成正文的目标汉字数（提示模型严格遵循该值，标点数字不计）",
    )
    title_count: int = Field(
        default=1,
        ge=1,
        le=10,
        description="需要生成的标题候选数量",
    )
    body_count: int = Field(
        default=1,
        ge=1,
        le=10,
        description="需要生成的正文候选数量",
    )

    @field_validator("selling_point_catalog_id")
    @classmethod
    def normalize_catalog_id(cls, value: str | None) -> str | None:
        """去除目录 ID 首尾空白。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("manual_selling_point")
    @classmethod
    def normalize_manual_selling_point(cls, value: str | None) -> str | None:
        """去除直接输入卖点的首尾空白。"""
        return _trimmed(value)

    @field_validator("product_identifiers", mode="before")
    @classmethod
    def normalize_product_identifiers(cls, value: object) -> list[str]:
        """归一化商品 ID 列表：去重（不区分大小写）、限 20 个。"""
        if not isinstance(value, list):
            raise ValueError("商品 ID 或货号必须是列表")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            identifier = str(item).strip()
            if not identifier:
                continue
            if len(identifier) > 100:
                raise ValueError("单个商品 ID 或货号不能超过 100 个字符")
            key = identifier.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(identifier)
        if len(normalized) > 20:
            raise ValueError("一次最多支持 20 个商品 ID 或货号")
        return normalized

    @field_validator("product_urls", mode="before")
    @classmethod
    def normalize_product_urls(cls, value: object) -> list[str]:
        """归一化商品链接列表（可选）。"""
        return _normalize_product_urls(value, required=False)

    @field_validator("festival")
    @classmethod
    def normalize_festival(cls, value: str | None) -> str | None:
        """去首尾空白。"""
        return _trimmed(value)

    @field_validator("custom_style", "custom_scene", "custom_festival")
    @classmethod
    def normalize_custom_copy_context(cls, value: str | None) -> str | None:
        """去除自定义生成条件的首尾空白，空输入不覆盖预设。"""
        return _trimmed(value)

    @model_validator(mode="after")
    def require_product_for_search_config(self) -> "GenerateCopyRequest":
        """校验必填生成条件以及商品搜索服务配置。"""
        if self.selling_point_input_mode == SellingPointInputMode.EXCEL:
            if not self.selling_point_catalog_id or len(self.selling_point_catalog_id) < 16:
                raise ValueError("请先上传商品核心卖点 Excel")
            if not self.product_identifiers:
                raise ValueError("至少需要输入一个商品 ID 或货号")
            if self.manual_selling_point:
                raise ValueError("Excel 模式不能同时提交直接输入的商品核心卖点")
        else:
            if not self.manual_selling_point:
                raise ValueError("请填写商品核心卖点")
            if self.selling_point_catalog_id or self.product_identifiers:
                raise ValueError("直接输入模式不能同时提交卖点 Excel 或商品 ID")
        if not self.style and not self.custom_style:
            raise ValueError("请选择或填写文案风格")
        if not self.scene and not self.custom_scene:
            raise ValueError("请选择或填写内容场景")
        if not self.product_urls and (
            self.product_search.endpoint_url or self.product_search.api_key
        ):
            raise ValueError("配置商品搜索服务前，请先填写至少一个商品链接")
        return self


class GeneratedCopyDraft(BaseModel):
    """LLM 生成的文案草稿（用于解析 LLM 返回的 JSON）。

    固定上下限用于限制单次模型响应的体积；标题目标字数由服务层另行严格校验。
    """

    model_config = ConfigDict(extra="forbid")

    titles: list[str] = Field(min_length=1, max_length=10)
    bodies: list[str] = Field(min_length=1, max_length=10)

    @field_validator("titles")
    @classmethod
    def normalize_titles(cls, values: list[str]) -> list[str]:
        """去除标题首尾空白并维持单条标题的固定长度边界。"""
        return cls._normalize_candidates(values, "标题", minimum=2, maximum=200)

    @field_validator("bodies")
    @classmethod
    def normalize_bodies(cls, values: list[str]) -> list[str]:
        """去除正文首尾空白并维持单条正文的固定长度边界。"""
        return cls._normalize_candidates(values, "正文", minimum=10, maximum=2000)

    @staticmethod
    def _normalize_candidates(
        values: list[str], label: str, *, minimum: int, maximum: int
    ) -> list[str]:
        normalized: list[str] = []
        for index, value in enumerate(values, start=1):
            text = value.strip()
            if not text:
                raise ValueError(f"第 {index} 条{label}不能为空")
            if not minimum <= len(text) <= maximum:
                raise ValueError(
                    f"第 {index} 条{label}长度必须在 {minimum}-{maximum} 个字符之间"
                )
            normalized.append(text)
        return normalized


class GenerateCopyResponse(BaseModel):
    """文案生成响应。"""

    title: str                              # 生成的标题
    body: str                               # 生成的正文
    titles: list[str]                       # 全部标题候选
    bodies: list[str]                       # 全部正文候选
    provider: str                           # 使用的供应商
    model: str                              # 使用的模型
    selling_point_references: list[SellingPointReference]  # 引用的卖点
    product_references: list[ProductReference] = Field(default_factory=list)  # 引用的商品资料
    title_max_chars: int                    # 标题目标字符数（兼容旧字段名）
    body_max_chars: int                     # 正文目标字符数（兼容旧字段名）
    title_count: int                        # 标题候选数量
    body_count: int                         # 正文候选数量

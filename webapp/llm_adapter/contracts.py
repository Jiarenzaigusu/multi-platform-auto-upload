from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from webapp.llm_adapter.catalog import (
    AdapterKind,
    normalize_qwen_base_url,
)


class ActivateAdapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AdapterKind
    api_key: str = Field(min_length=8, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2048)

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("API Key 至少需要 8 个字符")
        return normalized

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_qwen_base_url(value)

    @model_validator(mode="after")
    def validate_provider_endpoint(self) -> "ActivateAdapterRequest":
        if self.provider is not AdapterKind.QWEN and self.base_url:
            raise ValueError("只有千问适配器支持自定义 OpenAI 兼容地址")
        return self


class AdapterOption(BaseModel):
    provider: AdapterKind
    label: str
    description: str
    model: str
    model_label: str
    endpoint: str
    key_hint: str
    configured: bool = False


class ActiveAdapter(BaseModel):
    provider: AdapterKind
    label: str
    model: str
    model_label: str
    endpoint: str


class AdapterStatus(BaseModel):
    adapters: list[AdapterOption]
    active: ActiveAdapter | None = None

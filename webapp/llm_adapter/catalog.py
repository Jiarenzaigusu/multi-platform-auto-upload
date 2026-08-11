from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from urllib.parse import urlsplit


class AdapterKind(str, Enum):
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    DOUBAO = "doubao"


@dataclass(frozen=True, slots=True)
class AdapterDefinition:
    kind: AdapterKind
    label: str
    description: str
    base_url: str
    model: str
    model_label: str
    key_hint: str


QWEN_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_PUBLIC_HOSTS = {
    "dashscope.aliyuncs.com",
    "dashscope-intl.aliyuncs.com",
}


def normalize_qwen_base_url(value: str) -> str:
    """Allow API keys to be sent only to known DashScope-compatible hosts."""
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("千问 OpenAI 兼容地址格式无效") from exc

    hostname = (parsed.hostname or "").lower()
    trusted_host = hostname in QWEN_PUBLIC_HOSTS or hostname.endswith(
        ".maas.aliyuncs.com"
    )
    if (
        parsed.scheme.lower() != "https"
        or not trusted_host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("千问地址必须是阿里云百炼提供的 HTTPS OpenAI 兼容地址")
    if parsed.path.rstrip("/") != "/compatible-mode/v1":
        raise ValueError("千问地址必须以 /compatible-mode/v1 结尾")
    return f"https://{hostname}/compatible-mode/v1"


def resolve_adapter_definition(
    kind: AdapterKind, base_url: str | None = None
) -> AdapterDefinition:
    definition = ADAPTER_CATALOG[kind]
    if not base_url:
        return definition
    if kind is not AdapterKind.QWEN:
        raise ValueError("只有千问适配器支持自定义 OpenAI 兼容地址")
    return replace(definition, base_url=normalize_qwen_base_url(base_url))


ADAPTER_CATALOG: dict[AdapterKind, AdapterDefinition] = {
    AdapterKind.DEEPSEEK: AdapterDefinition(
        kind=AdapterKind.DEEPSEEK,
        label="DeepSeek",
        description="官方深度推理模型，增强复杂中文写作与结构化生成。",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-reasoner",
        model_label="DeepSeek Reasoner",
        key_hint="sk-...",
    ),
    AdapterKind.QWEN: AdapterDefinition(
        kind=AdapterKind.QWEN,
        label="千问",
        description="阿里云百炼兼容接口，兼顾中文表达与工具调用。",
        base_url=QWEN_DEFAULT_BASE_URL,
        model="qwen-plus",
        model_label="Qwen Plus",
        key_hint="sk-... 或 sk-ws-...",
    ),
    AdapterKind.DOUBAO: AdapterDefinition(
        kind=AdapterKind.DOUBAO,
        label="豆包",
        description="同代 Turbo 模型，兼顾中文内容质量、响应速度与调用成本。",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seed-2-1-turbo-260628",
        model_label="Doubao Seed 2.1 Turbo",
        key_hint="输入火山方舟 API Key",
    ),
}

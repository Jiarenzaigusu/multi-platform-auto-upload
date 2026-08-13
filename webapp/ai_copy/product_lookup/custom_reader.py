"""webapp.ai_copy.product_lookup.custom_reader 模块：专用商品搜索服务读取器。

当公开商品页无法直接解析时，用户可配置专用商品搜索服务（HTTP POST）。
本模块向配置的 endpoint 发送 {url: product_url} 请求，支持 Bearer Token 鉴权，
解析返回的 JSON（title/summary/attributes）构造 ProductReference。

API Key 不会写入项目状态或运行目录，仅用于本次请求。
"""
from __future__ import annotations

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from webapp.ai_copy.contracts import ProductReference, ProductSearchConfig
from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.interfaces import compact_text
from webapp.ai_copy.product_lookup.public_http import create_trusted_ssl_context


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CustomProductServiceReader:
    MAX_RESPONSE_BYTES = 500_000

    def __init__(
        self,
        *,
        timeout_seconds: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context or create_trusted_ssl_context()

    def inspect(
        self, product_url: str, config: ProductSearchConfig
    ) -> ProductReference:
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        request = Request(
            str(config.endpoint_url),
            data=json.dumps({"url": product_url}, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        opener = build_opener(
            _NoRedirectHandler(), HTTPSHandler(context=self._ssl_context)
        )
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ProductLookupError(
                f"商品搜索服务返回 HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProductLookupError(f"无法连接商品搜索服务：{exc}") from exc
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise ProductLookupError("商品搜索服务响应超过 500 KB 限制")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProductLookupError("商品搜索服务没有返回有效 JSON") from exc
        if not isinstance(document, dict):
            raise ProductLookupError("商品搜索服务响应必须是 JSON 对象")

        title = compact_text(document.get("title"), 300)
        summary = compact_text(document.get("summary"), 4000)
        attributes = document.get("attributes", {})
        if not title or not summary or not isinstance(attributes, dict):
            raise ProductLookupError(
                "商品搜索服务必须返回 title、summary 和 attributes 对象"
            )
        return ProductReference(
            source_url=product_url,
            title=title,
            summary=summary,
            attributes={
                compact_text(key, 80): compact_text(value, 300)
                for key, value in attributes.items()
                if compact_text(key, 80) and compact_text(value, 300)
            },
        )

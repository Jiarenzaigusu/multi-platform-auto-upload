from __future__ import annotations

import json
import ssl

from webapp.ai_copy.contracts import ProductReference, ProductSearchConfig
from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.interfaces import compact_text
from webapp.ai_copy.product_lookup.public_http import PublicPageHttpClient


class CustomProductServiceReader:
    MAX_RESPONSE_BYTES = 500_000

    def __init__(
        self,
        *,
        timeout_seconds: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._client = PublicPageHttpClient(
            timeout_seconds=timeout_seconds,
            max_bytes=self.MAX_RESPONSE_BYTES,
            ssl_context=ssl_context,
            max_redirects=0,
        )

    def inspect(
        self, product_url: str, config: ProductSearchConfig
    ) -> ProductReference:
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        response = self._client.post_json(
            str(config.endpoint_url),
            json.dumps({"url": product_url}, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        raw = response.content
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

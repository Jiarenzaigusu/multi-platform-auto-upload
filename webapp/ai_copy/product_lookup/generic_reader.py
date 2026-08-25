from __future__ import annotations

from html.parser import HTMLParser
import json
from typing import Any

from webapp.ai_copy.contracts import ProductReference
from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.interfaces import compact_text
from webapp.ai_copy.product_lookup.public_http import PublicPageHttpClient


class _ProductHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.in_json_ld = False
        self.title_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self.json_ld_documents: list[str] = []
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "")
            if name and content:
                self.metadata[name] = content
        elif (
            tag.lower() == "script"
            and attributes.get("type", "").lower() == "application/ld+json"
        ):
            self.in_json_ld = True
            self.json_ld_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False
        elif tag.lower() == "script" and self.in_json_ld:
            self.in_json_ld = False
            self.json_ld_documents.append("".join(self.json_ld_parts))

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_json_ld:
            self.json_ld_parts.append(data)


def _find_product_schema(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            found = _find_product_schema(item)
            if found:
                return found
    elif isinstance(value, dict):
        schema_type = value.get("@type")
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if any(str(item).lower() == "product" for item in types):
            return value
        for item in value.values():
            found = _find_product_schema(item)
            if found:
                return found
    return None


class GenericHtmlProductReader:
    def __init__(self, http_client: PublicPageHttpClient) -> None:
        self._http_client = http_client

    @staticmethod
    def supports(_product_url: str) -> bool:
        return True

    def inspect(self, product_url: str) -> ProductReference:
        page = self._http_client.get(product_url)
        if page.content_type not in {"text/html", "application/xhtml+xml"}:
            raise ProductLookupError("商品链接返回的不是 HTML 页面")

        try:
            page_text = page.content.decode(page.charset, errors="replace")
        except LookupError:
            page_text = page.content.decode("utf-8", errors="replace")

        parser = _ProductHTMLParser()
        parser.feed(page_text)
        product_schema: dict[str, Any] = {}
        for raw_schema in parser.json_ld_documents:
            try:
                product_schema = _find_product_schema(json.loads(raw_schema)) or {}
            except json.JSONDecodeError:
                continue
            if product_schema:
                break

        title = compact_text(
            product_schema.get("name")
            or parser.metadata.get("og:title")
            or " ".join(parser.title_parts),
            300,
        )
        summary = compact_text(
            product_schema.get("description")
            or parser.metadata.get("og:description")
            or parser.metadata.get("description"),
            4000,
        )
        if not title or not summary:
            raise ProductLookupError(
                "商品页面没有可读取的标题或描述；请粘贴可直接访问的天猫或京东商品链接"
            )

        attributes: dict[str, str] = {}
        brand = product_schema.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        if brand:
            attributes["品牌"] = compact_text(brand, 300)
        if product_schema.get("sku"):
            attributes["SKU"] = compact_text(product_schema["sku"], 300)
        offers = product_schema.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict) and offers.get("price"):
            price = compact_text(offers["price"], 100)
            currency = compact_text(offers.get("priceCurrency"), 20)
            attributes["价格"] = f"{price} {currency}".strip()

        return ProductReference(
            source_url=product_url,
            title=title,
            summary=summary,
            attributes=attributes,
        )

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from webapp.ai_copy.contracts import ProductReference
from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.cache import ProductReferenceCache
from webapp.ai_copy.product_lookup.interfaces import compact_text
from webapp.ai_copy.product_lookup.tmall_client import TmallPageFetcher


TMALL_HOSTS = {"detail.tmall.com", "detail.m.tmall.com", "detail.tmall.hk"}
SUMMARY_FACTS = (
    "品牌",
    "材质成分",
    "面料",
    "版型分类",
    "适用性别",
    "上市年份季节",
    "袖长",
    "风格",
    "功能",
    "领型设计",
)


def is_tmall_product_url(product_url: str) -> bool:
    parsed = urlsplit(product_url)
    return (
        (parsed.hostname or "").lower() in TMALL_HOSTS
        and parsed.path.rstrip("/") == "/item.htm"
    )


def extract_tmall_product_ids(product_url: str) -> tuple[str, str | None] | None:
    if not is_tmall_product_url(product_url):
        return None
    query = parse_qs(urlsplit(product_url).query)
    item_id = query.get("id", [""])[0]
    sku_id = query.get("skuId", [None])[0]
    if not item_id.isdigit() or (sku_id is not None and not sku_id.isdigit()):
        return None
    return item_id, sku_id


def _extract_balanced_object(page_text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(page_text)):
        character = page_text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return page_text[start : index + 1]
    return None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tmall_response(page_text: str) -> dict[str, Any]:
    search_from = 0
    while True:
        app_data_start = page_text.find('{"appData"', search_from)
        if app_data_start < 0:
            return {}
        raw_object = _extract_balanced_object(page_text, app_data_start)
        search_from = app_data_start + 1
        if not raw_object or '"loaderData"' not in raw_object:
            continue
        try:
            payload = json.loads(raw_object)
        except json.JSONDecodeError:
            continue
        home = _mapping(_mapping(payload.get("loaderData")).get("home"))
        response = _mapping(_mapping(home.get("data")).get("res"))
        if response:
            return response


def _selected_sku_values(response: dict[str, Any], sku_id: str | None) -> list[str]:
    if not sku_id:
        return []
    sku_base = _mapping(response.get("skuBase"))
    skus = sku_base.get("skus")
    selected = (
        next(
            (
                sku
                for sku in skus
                if isinstance(sku, dict) and str(sku.get("skuId")) == sku_id
            ),
            None,
        )
        if isinstance(skus, list)
        else None
    )
    if not selected:
        return []

    selected_paths: dict[str, str] = {}
    for pair in str(selected.get("propPath", "")).split(";"):
        property_id, separator, value_id = pair.partition(":")
        if separator and property_id and value_id:
            selected_paths[property_id] = value_id

    values: list[str] = []
    props = sku_base.get("props")
    if not isinstance(props, list):
        return values
    for prop in props:
        if not isinstance(prop, dict):
            continue
        property_id = str(prop.get("pid", ""))
        value_id = selected_paths.get(property_id)
        value = _mapping(_mapping(prop.get("valueMap")).get(value_id))
        name = compact_text(prop.get("name"), 50)
        selected_name = compact_text(value.get("name"), 200)
        if name and selected_name:
            values.append(f"当前{name}：{selected_name}")
    return values


def _product_facts(response: dict[str, Any]) -> list[str]:
    industry = _mapping(_mapping(response.get("plusViewVO")).get("industryParamVO"))
    fact_map: dict[str, str] = {}
    for list_name in ("enhanceParamList", "basicParamList"):
        entries = industry.get(list_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = compact_text(entry.get("propertyName"), 50)
            value = compact_text(entry.get("valueName"), 300)
            if name in SUMMARY_FACTS and value and name not in fact_map:
                fact_map[name] = value
    return [f"{name}：{fact_map[name]}" for name in SUMMARY_FACTS if name in fact_map]


class TmallProductReader:
    def __init__(
        self,
        page_fetcher: TmallPageFetcher,
        cache: ProductReferenceCache | None = None,
    ) -> None:
        self._page_fetcher = page_fetcher
        self._cache = cache or ProductReferenceCache(
            fresh_seconds=600,
            stale_seconds=3_600,
        )

    @staticmethod
    def supports(product_url: str) -> bool:
        return is_tmall_product_url(product_url)

    def inspect(self, product_url: str) -> ProductReference:
        product_ids = extract_tmall_product_ids(product_url)
        if not product_ids:
            raise ProductLookupError("无法从天猫商品链接中识别商品 ID 或 SKU ID")
        item_id, sku_id = product_ids
        cache_key = f"{item_id}:{sku_id or ''}"
        cached = self._cache.get_fresh(cache_key)
        if cached:
            return cached.model_copy(update={"source_url": product_url})

        try:
            reference = self._inspect_uncached(product_url, item_id, sku_id)
        except ProductLookupError:
            stale = self._cache.get_stale(cache_key)
            if stale:
                return stale.model_copy(update={"source_url": product_url})
            raise
        self._cache.put(cache_key, reference)
        return reference

    def _inspect_uncached(
        self, product_url: str, item_id: str, sku_id: str | None
    ) -> ProductReference:
        page = self._page_fetcher.get(product_url)
        if page.content_type not in {"text/html", "application/xhtml+xml"}:
            raise ProductLookupError("天猫商品链接返回的不是 HTML 页面")
        try:
            page_text = page.content.decode(page.charset, errors="replace")
        except LookupError:
            page_text = page.content.decode("utf-8", errors="replace")

        response = _tmall_response(page_text)
        item = _mapping(response.get("item"))
        if str(item.get("itemId", "")) != item_id:
            raise ProductLookupError(
                "天猫商品页面没有返回可解析的商品数据；请确认账号登录状态和商品链接"
            )
        title = compact_text(item.get("title"), 300)
        if not title:
            raise ProductLookupError("天猫商品页面没有返回商品标题")

        summary_parts = [
            *_selected_sku_values(response, sku_id),
            *_product_facts(response),
        ]
        details = "；".join(summary_parts)
        summary = compact_text(f"{title}。{details}" if details else title, 4000)
        return ProductReference(
            source_url=product_url,
            title=title,
            summary=summary,
            attributes={},
        )


__all__ = [
    "TmallProductReader",
    "extract_tmall_product_ids",
    "is_tmall_product_url",
]

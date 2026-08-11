from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from webapp.ai_copy.contracts import ProductReference
from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.cache import ProductReferenceCache
from webapp.ai_copy.product_lookup.interfaces import compact_text
from webapp.ai_copy.product_lookup.jd_client import JD_MOBILE_HEADERS, JdPageFetcher
JD_HOSTS = {"item.jd.com", "item.m.jd.com"}
JD_SKU_PATHS = (
    re.compile(r"^/(?P<sku>\d+)\.html$"),
    re.compile(r"^/product/(?P<sku>\d+)\.html$"),
)


def extract_jd_sku(product_url: str) -> str | None:
    parsed = urlsplit(product_url)
    if (parsed.hostname or "").lower() not in JD_HOSTS:
        return None
    for pattern in JD_SKU_PATHS:
        matched = pattern.fullmatch(parsed.path)
        if matched:
            return matched.group("sku")
    return None


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


def _read_window_object(page_text: str, variable_name: str) -> dict[str, Any]:
    marker_index = page_text.find(f"window.{variable_name}")
    if marker_index < 0:
        return {}
    assignment_index = page_text.find("=", marker_index)
    object_start = page_text.find("{", assignment_index)
    if assignment_index < 0 or object_start < 0:
        return {}
    raw_object = _extract_balanced_object(page_text, object_start)
    if not raw_object:
        return {}
    try:
        value = json.loads(raw_object)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _product_features(item: dict[str, Any]) -> dict[str, str]:
    feature_text = _mapping(item.get("spAttr")).get("product_features")
    if not isinstance(feature_text, str):
        return {}
    features: dict[str, str] = {}
    for raw_feature in feature_text.split(";"):
        key, separator, value = raw_feature.partition(":")
        if separator and key and value:
            features[key] = value
    return features


def _selected_variant(item: dict[str, Any], sku: str) -> dict[str, Any]:
    variants = item.get("newColorSize")
    if not isinstance(variants, list):
        return {}
    for variant in variants:
        if isinstance(variant, dict) and str(variant.get("skuId")) == sku:
            return variant
    return {}


def _product_title(
    item: dict[str, Any], item_info: dict[str, Any], variant: dict[str, Any]
) -> str:
    detailed_title = compact_text(item_info.get("skuName"), 300)
    if detailed_title:
        return detailed_title

    listed_title = compact_text(item.get("skuName"), 300)
    if listed_title and not listed_title.endswith("..."):
        return listed_title

    brand = compact_text(item.get("brandName"), 100)
    base_name = compact_text(_product_features(item).get("nameWithoutBrand"), 240)
    title_parts = [part for part in (brand, base_name) if part]
    for value in (variant.get("color"), variant.get("size")):
        normalized = compact_text(value, 100)
        if normalized and not any(normalized in part for part in title_parts):
            title_parts.append(normalized)
    return compact_text(" ".join(title_parts) or listed_title, 300)


class JdProductReader:
    def __init__(
        self,
        page_fetcher: JdPageFetcher,
        cache: ProductReferenceCache | None = None,
    ) -> None:
        self._page_fetcher = page_fetcher
        self._cache = cache or ProductReferenceCache(
            fresh_seconds=600,
            stale_seconds=3_600,
        )

    @staticmethod
    def supports(product_url: str) -> bool:
        return extract_jd_sku(product_url) is not None

    def inspect(self, product_url: str) -> ProductReference:
        sku = extract_jd_sku(product_url)
        if not sku:
            raise ProductLookupError("无法从京东商品链接中识别商品 ID")

        cached = self._cache.get_fresh(sku)
        if cached:
            return cached.model_copy(update={"source_url": product_url})

        try:
            reference = self._inspect_uncached(product_url, sku)
        except ProductLookupError:
            stale = self._cache.get_stale(sku)
            if stale:
                return stale.model_copy(update={"source_url": product_url})
            raise
        self._cache.put(sku, reference)
        return reference

    def _inspect_uncached(self, product_url: str, sku: str) -> ProductReference:
        mobile_url = f"https://item.m.jd.com/product/{sku}.html"
        page = self._page_fetcher.get(mobile_url, headers=JD_MOBILE_HEADERS)
        if page.content_type not in {"text/html", "application/xhtml+xml"}:
            raise ProductLookupError("京东商品链接返回的不是 HTML 页面")
        try:
            page_text = page.content.decode(page.charset, errors="replace")
        except LookupError:
            page_text = page.content.decode("utf-8", errors="replace")

        item_only = _mapping(_read_window_object(page_text, "_itemOnly").get("item"))
        item_info = _mapping(_read_window_object(page_text, "_itemInfo").get("product"))
        variant = _selected_variant(item_only, sku)
        title = _product_title(item_only, item_info, variant)
        if not title:
            raise ProductLookupError(
                "京东商品页面没有返回可解析的商品数据；请确认商品存在，或稍后重试"
            )

        brand = compact_text(item_only.get("brandName"), 300)
        color = compact_text(item_info.get("color") or variant.get("color"), 300)
        size = compact_text(item_info.get("size") or variant.get("size"), 300)

        summary_parts = []
        if brand:
            summary_parts.append(f"品牌：{brand}")
        if color:
            summary_parts.append(f"当前颜色：{color}")
        if size:
            summary_parts.append(f"当前尺码：{size}")
        details = "；".join(summary_parts)
        summary = compact_text(f"{title}。{details}" if details else title, 4000)

        return ProductReference(
            source_url=product_url,
            title=title,
            summary=summary,
            attributes={},
        )

from __future__ import annotations

from typing import Protocol

from webapp.ai_copy.contracts import ProductReference
from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.cache import ProductReferenceCache
from webapp.ai_copy.product_lookup.tmall_reader import (
    extract_tmall_product_ids,
    is_tmall_product_url,
)


class AgentProductLookup(Protocol):
    def inspect_tmall_product(
        self, product_url: str, *, timeout_seconds: float
    ) -> ProductReference: ...


class AgentTmallProductReader:
    """Resolve Tmall products through the user's authenticated desktop agent."""

    def __init__(
        self,
        manager: AgentProductLookup,
        *,
        timeout_seconds: float,
        cache: ProductReferenceCache | None = None,
    ) -> None:
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._cache = cache or ProductReferenceCache(
            fresh_seconds=600, stale_seconds=3_600
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
            reference = self._manager.inspect_tmall_product(
                product_url, timeout_seconds=self._timeout_seconds
            )
        except ProductLookupError:
            stale = self._cache.get_stale(cache_key)
            if stale:
                return stale.model_copy(update={"source_url": product_url})
            raise
        self._cache.put(cache_key, reference)
        return reference


__all__ = ["AgentTmallProductReader"]

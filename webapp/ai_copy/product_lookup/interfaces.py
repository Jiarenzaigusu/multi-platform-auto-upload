from __future__ import annotations

import html
import re
from typing import Protocol, Sequence

from webapp.ai_copy.contracts import ProductReference, ProductSearchConfig
from webapp.ai_copy.errors import ProductLookupError


def compact_text(value: object, limit: int) -> str:
    text = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


class ProductPageReader(Protocol):
    def supports(self, product_url: str) -> bool: ...

    def inspect(self, product_url: str) -> ProductReference: ...


class ProductPageLookup(Protocol):
    def inspect(self, product_url: str) -> ProductReference: ...


class ProductLookup(Protocol):
    def inspect(
        self, product_url: str, config: ProductSearchConfig
    ) -> ProductReference: ...


class ProductReaderRouter:
    """Selects one product reader without exposing platform details upstream."""

    def __init__(self, readers: Sequence[ProductPageReader]) -> None:
        if not readers:
            raise ValueError("至少需要配置一个商品读取器")
        self._readers = tuple(readers)

    def inspect(self, product_url: str) -> ProductReference:
        for reader in self._readers:
            if reader.supports(product_url):
                return reader.inspect(product_url)
        raise ProductLookupError("没有可用于该商品链接的读取器")

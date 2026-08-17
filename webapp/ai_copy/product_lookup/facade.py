"""webapp.ai_copy.product_lookup.facade 模块：商品读取门面（ProductSearchTool）。

整合：
- page_router: 平台专属读取器路由（京东 → 天猫 → 通用 HTML）
- custom_reader: 专用商品搜索服务读取器

inspect() 根据是否配置搜索服务决定使用哪个读取器。
"""
from __future__ import annotations

from webapp.ai_copy.contracts import ProductReference, ProductSearchConfig
from webapp.ai_copy.product_lookup.cache import ProductReferenceCache
from webapp.ai_copy.product_lookup.custom_reader import CustomProductServiceReader
from webapp.ai_copy.product_lookup.generic_reader import GenericHtmlProductReader
from webapp.ai_copy.product_lookup.interfaces import (
    ProductLookup,
    ProductPageLookup,
    ProductReaderRouter,
)
from webapp.ai_copy.product_lookup.jd_client import PatchrightJdPageFetcher
from webapp.ai_copy.product_lookup.jd_reader import JdProductReader
from webapp.ai_copy.product_lookup.public_http import (
    PublicPageHttpClient,
    create_trusted_ssl_context,
)
from webapp.ai_copy.product_lookup.tmall_client import TmallPageFetcher
from webapp.ai_copy.product_lookup.tmall_reader import TmallProductReader
from webapp.ai_copy.settings import AiCopySettings


class ProductSearchTool:
    """Facade shared by product preview and the AI copy generation workflow."""

    def __init__(
        self,
        settings: AiCopySettings,
        *,
        page_router: ProductPageLookup | None = None,
        custom_reader: ProductLookup | None = None,
        tmall_page_fetcher: TmallPageFetcher | None = None,
    ) -> None:
        if page_router is None or custom_reader is None:
            ssl_context = create_trusted_ssl_context()
            http_client = PublicPageHttpClient(
                timeout_seconds=settings.product_timeout_seconds,
                max_bytes=settings.max_product_page_bytes,
                ssl_context=ssl_context,
            )
            platform_readers = [
                JdProductReader(
                    PatchrightJdPageFetcher(
                        timeout_seconds=settings.product_timeout_seconds,
                        max_bytes=settings.max_product_page_bytes,
                        max_attempts=settings.jd_request_attempts,
                        retry_base_seconds=settings.jd_retry_base_seconds,
                    ),
                    ProductReferenceCache(
                        fresh_seconds=settings.product_cache_seconds,
                        stale_seconds=settings.product_stale_cache_seconds,
                    ),
                )
            ]
            if tmall_page_fetcher is not None:
                platform_readers.append(
                    TmallProductReader(
                        tmall_page_fetcher,
                        ProductReferenceCache(
                            fresh_seconds=settings.product_cache_seconds,
                            stale_seconds=settings.product_stale_cache_seconds,
                        ),
                    )
                )
            platform_readers.append(GenericHtmlProductReader(http_client))
            page_router = page_router or ProductReaderRouter(platform_readers)
            custom_reader = custom_reader or CustomProductServiceReader(
                timeout_seconds=settings.product_timeout_seconds,
                ssl_context=ssl_context,
            )
        self._page_router = page_router
        self._custom_reader = custom_reader

    def inspect(
        self, product_url: str, config: ProductSearchConfig
    ) -> ProductReference:
        if config.endpoint_url:
            return self._custom_reader.inspect(product_url, config)
        return self._page_router.inspect(product_url)


__all__ = ["ProductSearchTool"]

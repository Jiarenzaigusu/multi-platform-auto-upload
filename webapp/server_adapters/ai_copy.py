"""Compose the pure-local AI Copy service with server execution boundaries."""
from __future__ import annotations

from webapp.ai_copy.product_lookup.agent_reader import AgentTmallProductReader
from webapp.ai_copy.product_lookup.custom_reader import CustomProductServiceReader
from webapp.ai_copy.product_lookup.facade import ProductSearchTool
from webapp.ai_copy.product_lookup.generic_reader import GenericHtmlProductReader
from webapp.ai_copy.product_lookup.interfaces import ProductReaderRouter
from webapp.ai_copy.product_lookup.jd_client import PatchrightJdPageFetcher
from webapp.ai_copy.product_lookup.jd_reader import JdProductReader
from webapp.ai_copy.product_lookup.public_http import (
    PublicPageHttpClient,
    create_trusted_ssl_context,
)
from webapp.ai_copy.product_lookup.tmall_client import TmallPageFetcher
from webapp.ai_copy.product_lookup.tmall_reader import TmallProductReader
from webapp.ai_copy.product_lookup.cache import ProductReferenceCache
from webapp.ai_copy.service import AiCopyService
from webapp.ai_copy.selling_points import SellingPointCatalogStore
from webapp.ai_copy.settings import AiCopySettings
from webapp.llm_adapter import LLMAdapterRegistry, OpenAICompatibleProvider


def build_server_ai_copy_service(
    registry: LLMAdapterRegistry,
    settings: AiCopySettings,
    *,
    tmall_page_fetcher: TmallPageFetcher | None = None,
    agent_tmall_reader: AgentTmallProductReader | None = None,
) -> AiCopyService:
    """Keep source AI behavior intact while choosing its product I/O boundary."""
    ssl_context = create_trusted_ssl_context()
    readers = [
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
    if agent_tmall_reader is not None:
        readers.append(agent_tmall_reader)
    elif tmall_page_fetcher is not None:
        readers.append(
            TmallProductReader(
                tmall_page_fetcher,
                ProductReferenceCache(
                    fresh_seconds=settings.product_cache_seconds,
                    stale_seconds=settings.product_stale_cache_seconds,
                ),
            )
        )
    readers.append(
        GenericHtmlProductReader(
            PublicPageHttpClient(
                timeout_seconds=settings.product_timeout_seconds,
                max_bytes=settings.max_product_page_bytes,
                ssl_context=ssl_context,
            )
        )
    )
    product_tool = ProductSearchTool(
        settings,
        page_router=ProductReaderRouter(readers),
        custom_reader=CustomProductServiceReader(
            timeout_seconds=settings.product_timeout_seconds,
            ssl_context=ssl_context,
        ),
    )
    return AiCopyService(
        OpenAICompatibleProvider(registry),
        product_tool,
        SellingPointCatalogStore(
            max_workbook_bytes=settings.max_selling_point_workbook_bytes,
            max_rows=settings.max_selling_point_rows,
            ttl_seconds=settings.selling_point_catalog_ttl_seconds,
            max_catalogs=settings.max_selling_point_catalogs,
        ),
    )

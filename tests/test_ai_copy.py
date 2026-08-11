from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import certifi
from fastapi import HTTPException, Request

from webapp.ai_copy.contracts import (
    GenerateCopyRequest,
    ProductReference,
    ProductReferenceRequest,
    ProductSearchConfig,
)
from webapp.ai_copy.errors import LLMResponseError, ProductLookupError
from webapp.ai_copy.product_lookup import ProductSearchTool
from webapp.ai_copy.product_lookup.cache import ProductReferenceCache
from webapp.ai_copy.product_lookup.generic_reader import GenericHtmlProductReader
from webapp.ai_copy.product_lookup.jd_client import (
    JD_MOBILE_HEADERS,
    PatchrightJdPageFetcher,
    _TransientJdRequestError,
)
from webapp.ai_copy.product_lookup.jd_reader import JdProductReader, extract_jd_sku
from webapp.ai_copy.product_lookup.public_http import (
    FetchedPage,
    PublicPageHttpClient,
    create_trusted_ssl_context,
    validate_public_product_url,
)
from webapp.ai_copy.product_lookup.tmall_client import BrowserRuntimeTmallPageFetcher
from webapp.ai_copy.product_lookup.tmall_reader import (
    TmallProductReader,
    extract_tmall_product_ids,
)
from webapp.ai_copy.router import create_ai_copy_router
from webapp.ai_copy.service import AiCopyService
from webapp.ai_copy.settings import AiCopySettings


class FakeChatProvider:
    model = "test-copy-model"
    provider_label = "Test Provider"

    def __init__(
        self,
        *,
        tool_url: str = "https://shop.example/product/42",
        ready: bool = True,
        draft: dict[str, str] | None = None,
    ) -> None:
        self.tool_url = tool_url
        self.ready = ready
        self.draft = draft or {
            "title": "轻盈入夏，每一步都自在",
            "body": "轻薄透气的日常鞋款，让通勤和周末出游都更轻松。自然好搭配，陪你舒服走过每一程。",
        }
        self.calls: list[dict] = []

    def chat(self, messages, **options):
        self.calls.append({"messages": messages, **options})
        if options.get("tools"):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-product-1",
                        "type": "function",
                        "function": {
                            "name": "inspect_product_link",
                            "arguments": json.dumps({"url": self.tool_url}),
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": json.dumps(self.draft, ensure_ascii=False),
        }

    @staticmethod
    def session():
        return nullcontext()


class FakeProductTool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProductSearchConfig]] = []

    def inspect(self, url: str, config: ProductSearchConfig) -> ProductReference:
        self.calls.append((url, config))
        return ProductReference(
            source_url=url,
            title="轻量透气休闲鞋",
            summary="网布鞋面，适合通勤与日常步行。",
            attributes={"颜色": "米白", "尺码": "35-40"},
        )


class TmallBrowserCapacityTests(unittest.TestCase):
    """Keep AI browser reads inside the process-wide browser capacity limit."""

    def test_page_fetcher_holds_global_slot_while_runtime_executes(self):
        events: list[str] = []
        expected = FetchedPage(b"page", "text/html", "utf-8", "https://example.com")

        class Slot:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, _exc_type, _exc, _traceback):
                events.append("exit")

        class Runtime:
            def run(self, coroutine):
                events.append("run")
                coroutine.close()
                return expected

        fetcher = BrowserRuntimeTmallPageFetcher(
            Runtime(),
            Mock(),
            timeout_seconds=5,
            max_bytes=1024,
            browser_slots=Slot(),
        )

        self.assertIs(fetcher.get("https://detail.tmall.com/item.htm?id=1"), expected)
        self.assertEqual(events, ["enter", "run", "exit"])


class AiCopyServiceTests(unittest.TestCase):
    def test_product_link_is_read_through_required_llm_tool_call(self):
        provider = FakeChatProvider()
        product_tool = FakeProductTool()
        service = AiCopyService(provider, product_tool)
        request = GenerateCopyRequest(
            content_brief="突出轻便、透气和通勤百搭",
            style="friendly",
            scene="short_video",
            festival="七夕",
            product_url="https://shop.example/product/42",
            product_search={
                "endpoint_url": "https://search.example/inspect",
                "api_key": "request-only-secret",
            },
        )

        result = service.generate(request)

        self.assertEqual(result.model, "test-copy-model")
        self.assertEqual(result.provider, "Test Provider")
        self.assertEqual(result.product_reference.title, "轻量透气休闲鞋")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["tool_choice"]["function"]["name"], "inspect_product_link")
        self.assertEqual(product_tool.calls[0][0], "https://shop.example/product/42")
        self.assertEqual(product_tool.calls[0][1].api_key, "request-only-secret")
        final_messages = provider.calls[1]["messages"]
        self.assertEqual(final_messages[-2]["role"], "tool")
        self.assertIn("轻量透气休闲鞋", final_messages[-2]["content"])

    def test_generation_without_product_link_does_not_run_product_tool(self):
        provider = FakeChatProvider()
        product_tool = FakeProductTool()
        service = AiCopyService(provider, product_tool)

        result = service.generate(
            GenerateCopyRequest(
                content_brief="夏季轻量通勤鞋",
                style="minimal",
                scene="social_post",
            )
        )

        self.assertIsNone(result.product_reference)
        self.assertEqual(product_tool.calls, [])
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn("tools", provider.calls[0])

    def test_llm_cannot_change_the_requested_product_url(self):
        provider = FakeChatProvider(tool_url="https://attacker.example/private")
        service = AiCopyService(provider, FakeProductTool())

        with self.assertRaisesRegex(LLMResponseError, "链接与用户请求不一致"):
            service.generate(
                GenerateCopyRequest(
                    content_brief="商品介绍",
                    style="professional",
                    scene="product_detail",
                    product_url="https://shop.example/product/42",
                )
            )

    def test_high_risk_claim_is_rejected_after_generation(self):
        provider = FakeChatProvider(
            draft={"title": "销量第一的选择", "body": "轻松搭配日常造型，通勤穿着也很自在。"}
        )
        service = AiCopyService(provider, FakeProductTool())

        with self.assertRaisesRegex(LLMResponseError, "高风险"):
            service.generate(
                GenerateCopyRequest(
                    content_brief="日常百搭休闲鞋",
                    style="friendly",
                    scene="short_video",
                )
            )

    def test_invented_numeric_claim_is_rejected(self):
        provider = FakeChatProvider(
            draft={"title": "轻盈日常鞋", "body": "采用99%轻量设计，通勤更自在。"}
        )
        service = AiCopyService(provider, FakeProductTool())

        with self.assertRaisesRegex(LLMResponseError, "没有的数字信息：99%"):
            service.generate(
                GenerateCopyRequest(
                    content_brief="突出轻便和通勤百搭",
                    style="friendly",
                    scene="short_video",
                )
            )

    def test_source_grounded_number_is_allowed(self):
        provider = FakeChatProvider(
            draft={"title": "轻盈日常鞋", "body": "鞋面含棉99%，通勤穿着自然舒适。"}
        )
        service = AiCopyService(provider, FakeProductTool())

        result = service.generate(
            GenerateCopyRequest(
                content_brief="鞋面含棉99%，适合日常通勤",
                style="friendly",
                scene="short_video",
            )
        )

        self.assertIn("99%", result.body)

    def test_product_url_number_does_not_ground_a_marketing_claim(self):
        provider = FakeChatProvider(
            tool_url="https://shop.example/product/99",
            draft={"title": "轻盈日常鞋", "body": "99%用户都会喜欢的通勤选择。"},
        )
        service = AiCopyService(provider, FakeProductTool())

        with self.assertRaisesRegex(LLMResponseError, "没有的数字信息：99%"):
            service.generate(
                GenerateCopyRequest(
                    content_brief="突出轻便和通勤百搭",
                    style="friendly",
                    scene="short_video",
                    product_url="https://shop.example/product/99",
                )
            )


class _FakeHeaders:
    @staticmethod
    def get_content_type() -> str:
        return "text/html"

    @staticmethod
    def get_content_charset() -> str:
        return "utf-8"


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = _FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.content if limit < 0 else self.content[:limit]


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def open(self, *_args, **_kwargs) -> _FakeResponse:
        return self.response


class ProductSearchToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AiCopySettings()

    def test_custom_search_service_uses_strict_contract_and_request_key(self):
        response = FetchedPage(
            json.dumps(
                {
                    "title": "夏季凉感床品",
                    "summary": "柔软亲肤，适合夏季卧室。",
                    "attributes": {"规格": "四件套"},
                },
                ensure_ascii=False,
            ).encode(),
            "application/json",
            "utf-8",
            "https://search.example/inspect",
        )
        with patch(
            "webapp.ai_copy.product_lookup.public_http.PublicPageHttpClient.post_json",
            return_value=response,
        ) as post_json:
            result = ProductSearchTool(self.settings).inspect(
                "https://shop.example/item/1",
                ProductSearchConfig(
                    endpoint_url="https://search.example/inspect",
                    api_key="one-request-key",
                ),
            )

        endpoint, payload = post_json.call_args.args
        self.assertEqual(endpoint, "https://search.example/inspect")
        self.assertEqual(
            post_json.call_args.kwargs["headers"]["Authorization"],
            "Bearer one-request-key",
        )
        self.assertEqual(json.loads(payload), {"url": "https://shop.example/item/1"})
        self.assertEqual(result.attributes, {"规格": "四件套"})

    def test_custom_search_key_requires_https_endpoint(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ProductSearchConfig(
                endpoint_url="http://search.example/inspect",
                api_key="one-request-key",
            )

    def test_custom_service_rejects_private_dns_results(self):
        with patch(
            "webapp.ai_copy.product_lookup.public_http.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaisesRegex(ProductLookupError, "内网"):
                ProductSearchTool(self.settings).inspect(
                    "https://shop.example/item/1",
                    ProductSearchConfig(
                        endpoint_url="https://search.example/inspect",
                        api_key="one-request-key",
                    ),
                )

    def test_public_page_retries_each_validated_address(self):
        attempts: list[str] = []

        class FakeConnection:
            def __init__(self, _host, _port, address, _timeout, _ssl_context):
                self.address = address

            def request(self, *_args, **_kwargs):
                attempts.append(self.address)
                if self.address == "203.0.113.10":
                    raise OSError("unreachable")

            @staticmethod
            def getresponse():
                response = _FakeResponse(b"<html></html>")
                response.status = 200
                return response

            @staticmethod
            def close():
                return None

        with patch(
            "webapp.ai_copy.product_lookup.public_http.validate_public_product_url",
            return_value=["203.0.113.10", "203.0.113.11"],
        ), patch(
            "webapp.ai_copy.product_lookup.public_http._PinnedHTTPSConnection",
            FakeConnection,
        ):
            page = PublicPageHttpClient(
                timeout_seconds=1, max_bytes=1000, ssl_context=Mock()
            ).get("https://shop.example/item/1")

        self.assertEqual(attempts, ["203.0.113.10", "203.0.113.11"])
        self.assertEqual(
            (page.content, page.content_type, page.charset),
            (b"<html></html>", "text/html", "utf-8"),
        )

    def test_public_url_rejects_embedded_credentials(self):
        with self.assertRaisesRegex(ProductLookupError, "公开"):
            validate_public_product_url("https://user:password@shop.example/item/1")

    def test_ssl_context_uses_certifi_ca_bundle(self):
        context = Mock()
        with patch(
            "webapp.ai_copy.product_lookup.public_http.ssl.create_default_context",
            return_value=context,
        ) as create_context:
            result = create_trusted_ssl_context()

        self.assertIs(result, context)
        create_context.assert_called_once_with(cafile=certifi.where())

    def test_public_page_extracts_product_json_ld(self):
        page = b"""
        <html><head><script type="application/ld+json">
        {"@type":"Product","name":"Everyday Sneaker","description":"Light mesh upper",
         "brand":{"name":"North Star"},"sku":"NS-42",
         "offers":{"price":"399","priceCurrency":"CNY"}}
        </script></head></html>
        """
        http_client = Mock()
        http_client.get.return_value = FetchedPage(
            page, "text/html", "utf-8", "https://shop.example/item/42"
        )
        result = GenericHtmlProductReader(http_client).inspect(
            "https://shop.example/item/42"
        )

        self.assertEqual(result.title, "Everyday Sneaker")
        self.assertEqual(result.attributes["品牌"], "North Star")
        self.assertEqual(result.attributes["价格"], "399 CNY")

    def test_public_page_rejects_private_dns_results(self):
        with patch(
            "webapp.ai_copy.product_lookup.public_http.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaisesRegex(ProductLookupError, "内网"):
                validate_public_product_url("https://shop.example/item/42")

    def test_jd_reader_uses_mobile_page_and_extracts_structured_product(self):
        page = """
        <script>
        window._itemOnly = ({
          "item": {
            "skuName": "盖璞儿童连帽抓绒卫衣 ...",
            "brandName": "盖璞（GAP）",
            "spAttr": {
              "product_features": "nameWithoutBrand:儿童连帽抓绒卫衣;descModel:1"
            },
            "saleProp": {"1": "颜色", "2": "尺码"},
            "salePropSeq": {
              "1": ["黄色", "灰色"],
              "2": ["120 cm", "130 cm"]
            },
            "newColorSize": [
              {"skuId": "10230424567386", "color": "灰色", "size": "120 cm"}
            ]
          }
        });
        window._itemInfo = ({
          "product": {}
        });
        </script>
        """.encode()
        http_client = Mock()
        http_client.get.return_value = FetchedPage(
            page,
            "text/html",
            "utf-8",
            "https://item.m.jd.com/product/10230424567386.html",
        )

        reader = JdProductReader(http_client)
        result = reader.inspect(
            "https://item.jd.com/10230424567386.html?sdx=tracking"
        )

        http_client.get.assert_called_once_with(
            "https://item.m.jd.com/product/10230424567386.html",
            headers=JD_MOBILE_HEADERS,
        )
        self.assertEqual(result.source_url, "https://item.jd.com/10230424567386.html?sdx=tracking")
        self.assertEqual(
            result.title, "盖璞（GAP） 儿童连帽抓绒卫衣 灰色 120 cm"
        )
        self.assertEqual(result.attributes, {})

        cached = reader.inspect(
            "https://item.jd.com/10230424567386.html?sdx=another-request"
        )
        self.assertEqual(
            cached.source_url,
            "https://item.jd.com/10230424567386.html?sdx=another-request",
        )
        self.assertEqual(http_client.get.call_count, 1)

    def test_jd_reader_uses_recent_success_when_refresh_is_limited(self):
        now = [0.0]
        cache = ProductReferenceCache(
            fresh_seconds=10,
            stale_seconds=100,
            clock=lambda: now[0],
        )
        page_fetcher = Mock()
        page_fetcher.get.return_value = FetchedPage(
            b'<script>window._itemOnly=({"item":{"skuName":"JD Product"}})</script>',
            "text/html",
            "utf-8",
            "https://item.m.jd.com/product/42.html",
        )
        reader = JdProductReader(page_fetcher, cache)
        first = reader.inspect("https://item.jd.com/42.html")
        now[0] = 11
        page_fetcher.get.side_effect = ProductLookupError("京东商品页面限制了当前读取请求")

        fallback = reader.inspect("https://item.jd.com/42.html?retry=1")

        self.assertEqual(first.title, "JD Product")
        self.assertEqual(fallback.title, "JD Product")
        self.assertEqual(fallback.source_url, "https://item.jd.com/42.html?retry=1")

    def test_jd_client_retries_transient_limit_response(self):
        expected = FetchedPage(
            b"<html></html>",
            "text/html",
            "utf-8",
            "https://item.m.jd.com/product/42.html",
        )
        fetcher = PatchrightJdPageFetcher(
            timeout_seconds=1,
            max_bytes=1000,
            max_attempts=3,
            retry_base_seconds=0.1,
        )
        with patch.object(
            fetcher,
            "_get_once",
            side_effect=[_TransientJdRequestError("limited"), expected],
        ) as get_once, patch(
            "webapp.ai_copy.product_lookup.jd_client.time.sleep"
        ) as sleep:
            result = fetcher.get("https://item.m.jd.com/product/42.html")

        self.assertIs(result, expected)
        self.assertEqual(get_once.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_jd_reader_recognizes_desktop_and_mobile_links(self):
        self.assertEqual(
            extract_jd_sku("https://item.jd.com/10230424567386.html?foo=bar"),
            "10230424567386",
        )
        self.assertEqual(
            extract_jd_sku(
                "https://item.m.jd.com/product/10230424567386.html"
            ),
            "10230424567386",
        )
        self.assertIsNone(extract_jd_sku("https://example.com/10230424567386.html"))

    def test_tmall_reader_extracts_selected_sku_and_product_facts(self):
        response = {
            "item": {
                "itemId": "1006533002222",
                "title": "Gap 纯棉宽松短袖 T 恤",
            },
            "skuBase": {
                "skus": [
                    {
                        "skuId": "6003757841492",
                        "propPath": "1627207:43948691702;20509:382156294",
                    }
                ],
                "props": [
                    {
                        "pid": "1627207",
                        "name": "颜色",
                        "valueMap": {
                            "43948691702": {"name": "淡粉色729157"}
                        },
                    },
                    {
                        "pid": "20509",
                        "name": "尺码",
                        "valueMap": {
                            "382156294": {"name": "170/92A(M) 亚洲尺码"}
                        },
                    },
                ],
            },
            "plusViewVO": {
                "industryParamVO": {
                    "enhanceParamList": [
                        {"propertyName": "材质成分", "valueName": "棉100%"},
                        {"propertyName": "版型分类", "valueName": "宽松型"},
                    ],
                    "basicParamList": [
                        {"propertyName": "品牌", "valueName": "Gap"},
                        {"propertyName": "颜色", "valueName": "不应读取全部颜色"},
                    ],
                }
            },
        }
        payload = {
            "appData": None,
            "loaderData": {"home": {"data": {"res": response}}},
        }
        page_fetcher = Mock()
        page_fetcher.get.return_value = FetchedPage(
            f"<script>var b = {json.dumps(payload, ensure_ascii=False)}</script>".encode(),
            "text/html",
            "utf-8",
            "https://detail.tmall.com/item.htm?id=1006533002222",
        )
        reader = TmallProductReader(page_fetcher)
        product_url = (
            "https://detail.tmall.com/item.htm?id=1006533002222"
            "&skuId=6003757841492&spm=tracking"
        )

        result = reader.inspect(product_url)

        self.assertEqual(result.source_url, product_url)
        self.assertEqual(result.title, "Gap 纯棉宽松短袖 T 恤")
        self.assertIn("当前颜色：淡粉色729157", result.summary)
        self.assertIn("当前尺码：170/92A(M) 亚洲尺码", result.summary)
        self.assertIn("品牌：Gap", result.summary)
        self.assertIn("材质成分：棉100%", result.summary)
        self.assertNotIn("不应读取全部颜色", result.summary)
        self.assertEqual(result.attributes, {})

        cached = reader.inspect(
            "https://detail.tmall.com/item.htm?id=1006533002222"
            "&skuId=6003757841492&spm=another"
        )
        self.assertEqual(page_fetcher.get.call_count, 1)
        self.assertIn("spm=another", cached.source_url)

    def test_tmall_reader_rejects_login_page(self):
        page_fetcher = Mock()
        page_fetcher.get.return_value = FetchedPage(
            b'<script>window._config_={"action":"login"}</script>',
            "text/html",
            "utf-8",
            "https://login.taobao.com/member/login.jhtml",
        )

        with self.assertRaisesRegex(ProductLookupError, "没有返回可解析"):
            TmallProductReader(page_fetcher).inspect(
                "https://detail.tmall.com/item.htm?id=1006533002222"
            )

    def test_tmall_reader_recognizes_desktop_and_mobile_links(self):
        self.assertEqual(
            extract_tmall_product_ids(
                "https://detail.tmall.com/item.htm?id=1006533002222"
                "&skuId=6003757841492"
            ),
            ("1006533002222", "6003757841492"),
        )
        self.assertEqual(
            extract_tmall_product_ids(
                "https://detail.m.tmall.com/item.htm?id=1006533002222"
            ),
            ("1006533002222", None),
        )
        self.assertIsNone(
            extract_tmall_product_ids(
                "https://detail.tmall.com/item.htm?id=invalid&skuId=123"
            )
        )
        self.assertIsNone(
            extract_tmall_product_ids(
                "https://example.com/item.htm?id=1006533002222"
            )
        )


class AiCopyRouterTests(unittest.TestCase):
    def test_options_expose_ui_choices_and_llm_readiness(self):
        service = AiCopyService(FakeChatProvider(ready=False), FakeProductTool())
        router = create_ai_copy_router(service)
        endpoint = next(route.endpoint for route in router.routes if route.path.endswith("/options"))

        body = endpoint(Request({"type": "http", "headers": []}))

        self.assertFalse(body["llm"]["ready"])
        self.assertEqual(body["llm"]["model"], "test-copy-model")
        self.assertEqual(body["llm"]["provider"], "Test Provider")
        self.assertIn({"value": "friendly", "label": "亲切种草"}, body["styles"])

    def test_product_lookup_errors_are_mapped_without_leaking_request_key(self):
        class FailingTool(FakeProductTool):
            def inspect(self, _url, _config):
                raise ProductLookupError("商品服务暂时不可用")

        service = AiCopyService(FakeChatProvider(), FailingTool())
        router = create_ai_copy_router(service)
        endpoint = next(
            route.endpoint for route in router.routes if route.path.endswith("/product-reference")
        )

        with self.assertRaises(HTTPException) as context:
            asyncio.run(
                endpoint(
                    ProductReferenceRequest(
                        product_url="https://shop.example/item/1",
                        search={"api_key": None},
                    ),
                    Request({"type": "http", "headers": []}),
                )
            )

        self.assertEqual(context.exception.status_code, 502)
        self.assertEqual(context.exception.detail, "商品服务暂时不可用")

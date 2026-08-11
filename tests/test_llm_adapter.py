from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import HTTPSHandler

from fastapi import HTTPException, Request
from pydantic import ValidationError

from webapp.llm_adapter.catalog import AdapterKind
from webapp.llm_adapter.contracts import ActivateAdapterRequest
from webapp.llm_adapter.credential_store import FileAdapterCredentialStore
from webapp.llm_adapter.errors import AdapterNotConfiguredError, AdapterServiceError
from webapp.llm_adapter.provider import OpenAICompatibleProvider, _NoRedirectHandler
from webapp.llm_adapter.registry import LLMAdapterRegistry
from webapp.llm_adapter.router import create_llm_adapter_router


class _FakeResponse:
    def __init__(self, document: dict) -> None:
        self._content = json.dumps(document).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self._content if limit < 0 else self._content[:limit]


class LLMAdapterRegistryTests(unittest.TestCase):
    def test_switching_provider_replaces_the_only_active_credential(self):
        registry = LLMAdapterRegistry()

        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        status = registry.activate(AdapterKind.QWEN, "qwen-secret")

        active = registry.snapshot()
        self.assertEqual(active.definition.kind, AdapterKind.QWEN)
        self.assertEqual(active.api_key, "qwen-secret")
        self.assertEqual(status.active.provider, AdapterKind.QWEN)
        self.assertNotIn("secret", status.model_dump_json())

    def test_qwen_workspace_endpoint_is_kept_in_active_snapshot(self):
        registry = LLMAdapterRegistry()
        endpoint = (
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/"
        )

        status = registry.activate(AdapterKind.QWEN, "qwen-secret", endpoint)

        active = registry.snapshot()
        self.assertEqual(
            active.definition.base_url,
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(status.active.endpoint, active.definition.base_url)

    def test_clear_removes_the_active_adapter(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DOUBAO, "doubao-secret")

        status = registry.clear()

        self.assertIsNone(registry.snapshot())
        self.assertIsNone(status.active)
        self.assertTrue(
            next(
                adapter
                for adapter in status.adapters
                if adapter.provider is AdapterKind.DOUBAO
            ).configured
        )

    def test_file_store_restores_saved_keys_active_provider_and_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "llm-adapter-credentials.json"
            registry = LLMAdapterRegistry(FileAdapterCredentialStore(credential_path))
            registry.activate(AdapterKind.QWEN, "qwen-secret")
            registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")

            restored = LLMAdapterRegistry(FileAdapterCredentialStore(credential_path))
            status = restored.status()

            self.assertEqual(
                stat.S_IMODE(credential_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(restored.snapshot().definition.kind, AdapterKind.DEEPSEEK)
            self.assertEqual(
                restored.saved_credentials(AdapterKind.QWEN).api_key,
                "qwen-secret",
            )
            self.assertEqual(
                {adapter.provider for adapter in status.adapters if adapter.configured},
                {AdapterKind.DEEPSEEK, AdapterKind.QWEN},
            )
            self.assertNotIn("secret", status.model_dump_json())

    def test_delete_removes_only_selected_key_and_deactivates_it(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        registry.activate(AdapterKind.QWEN, "qwen-secret")

        status = registry.delete_credentials(AdapterKind.QWEN)

        self.assertIsNone(status.active)
        self.assertFalse(
            next(
                adapter
                for adapter in status.adapters
                if adapter.provider is AdapterKind.QWEN
            ).configured
        )
        self.assertEqual(
            registry.saved_credentials(AdapterKind.DEEPSEEK).api_key,
            "deepseek-secret",
        )
        with self.assertRaisesRegex(AdapterNotConfiguredError, "尚未保存"):
            registry.saved_credentials(AdapterKind.QWEN)

    def test_provider_switch_does_not_block_an_existing_lease_snapshot(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        switching = threading.Event()
        switched = threading.Event()

        def switch_provider():
            switching.set()
            registry.activate(AdapterKind.QWEN, "qwen-secret")
            switched.set()

        with registry.lease() as active:
            worker = threading.Thread(target=switch_provider)
            worker.start()
            self.assertTrue(switching.wait(timeout=1))
            self.assertTrue(switched.wait(timeout=1))
            self.assertEqual(active.definition.kind, AdapterKind.DEEPSEEK)

        worker.join(timeout=1)
        self.assertEqual(registry.snapshot().definition.kind, AdapterKind.QWEN)


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_active_adapter_controls_endpoint_key_and_model(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        provider = OpenAICompatibleProvider(registry)
        response = _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

        opener = Mock()
        opener.open.return_value = response
        with patch(
            "webapp.llm_adapter.provider.build_opener", return_value=opener
        ) as build:
            message = provider.chat([{"role": "user", "content": "hello"}])

        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer deepseek-secret")
        self.assertEqual(payload["model"], "deepseek-reasoner")
        self.assertEqual(message["content"], "ok")
        self.assertIsInstance(build.call_args.args[0], _NoRedirectHandler)
        https_handler = next(
            handler
            for handler in build.call_args.args
            if isinstance(handler, HTTPSHandler)
        )
        self.assertGreater(len(https_handler._context.get_ca_certs()), 0)

    def test_deepseek_retries_one_transient_read_timeout(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        provider = OpenAICompatibleProvider(registry)
        opener = Mock()
        opener.open.side_effect = [
            TimeoutError("The read operation timed out"),
            _FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            ),
        ]

        with patch(
            "webapp.llm_adapter.provider.build_opener", return_value=opener
        ), patch("webapp.llm_adapter.provider.time.sleep") as wait:
            message = provider.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(message["content"], "ok")
        self.assertEqual(opener.open.call_count, 2)
        wait.assert_called_once_with(1)

    def test_doubao_uses_current_ark_turbo_chat_model(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DOUBAO, "doubao-secret")
        provider = OpenAICompatibleProvider(registry)
        opener = Mock()
        opener.open.return_value = _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

        with patch(
            "webapp.llm_adapter.provider.build_opener", return_value=opener
        ):
            provider.chat([{"role": "user", "content": "hello"}])

        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(
            request.full_url,
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        )
        self.assertEqual(payload["model"], "doubao-seed-2-1-turbo-260628")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])

    def test_doubao_keeps_chat_function_call_and_structured_output_contract(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DOUBAO, "doubao-secret")
        provider = OpenAICompatibleProvider(registry)
        opener = Mock()
        opener.open.return_value = _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-product-1",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect_product_link",
                                        "arguments": '{"url":"https://shop.example/42"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "inspect_product_link",
                    "description": "读取商品",
                    "parameters": {"type": "object"},
                },
            }
        ]

        with patch(
            "webapp.llm_adapter.provider.build_opener", return_value=opener
        ):
            assistant = provider.chat(
                [{"role": "user", "content": "读取商品"}],
                tools=tools,
                tool_choice={
                    "type": "function",
                    "function": {"name": "inspect_product_link"},
                },
            )

        first_payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(first_payload["tools"], tools)
        self.assertEqual(
            first_payload["tool_choice"],
            {"type": "function", "function": {"name": "inspect_product_link"}},
        )
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-product-1")

        opener.open.return_value = _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"title":"标题","body":"正文"}',
                        }
                    }
                ]
            }
        )
        with patch(
            "webapp.llm_adapter.provider.build_opener", return_value=opener
        ):
            message = provider.chat(
                [
                    {"role": "system", "content": "系统提示"},
                    {"role": "user", "content": "读取商品"},
                    assistant,
                    {
                        "role": "tool",
                        "tool_call_id": "call-product-1",
                        "content": '{"title":"商品"}',
                    },
                    {"role": "user", "content": "生成结果"},
                ],
                response_format={"type": "json_object"},
            )

        second_payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(second_payload["messages"][2], assistant)
        self.assertEqual(second_payload["messages"][3]["role"], "tool")
        self.assertEqual(second_payload["response_format"], {"type": "json_object"})
        self.assertEqual(message["content"], '{"title":"标题","body":"正文"}')

    def test_probe_uses_prepared_workspace_credentials_without_activating_them(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        credentials = registry.prepare(
            AdapterKind.QWEN,
            "workspace-secret",
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
        provider = OpenAICompatibleProvider(registry)
        opener = Mock()
        opener.open.return_value = _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "O"}}]}
        )

        with patch(
            "webapp.llm_adapter.provider.build_opener", return_value=opener
        ):
            provider.probe(credentials)

        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(
            request.full_url,
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(registry.snapshot().definition.kind, AdapterKind.DEEPSEEK)

    def test_call_without_active_adapter_fails_before_network(self):
        provider = OpenAICompatibleProvider(LLMAdapterRegistry())

        with self.assertRaisesRegex(AdapterNotConfiguredError, "尚未启用"):
            provider.chat([{"role": "user", "content": "hello"}])

    def test_generation_session_keeps_one_provider_while_registry_switches(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        provider = OpenAICompatibleProvider(registry)
        opener = Mock()
        opener.open.return_value = _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

        with patch("webapp.llm_adapter.provider.build_opener", return_value=opener):
            with provider.session():
                provider.chat([{"role": "user", "content": "first"}])
                registry.activate(AdapterKind.QWEN, "qwen-secret")
                provider.chat([{"role": "user", "content": "second"}])
            provider.chat([{"role": "user", "content": "third"}])

        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertTrue(all("api.deepseek.com" in request.full_url for request in requests[:2]))
        self.assertTrue(
            all(request.get_header("Authorization") == "Bearer deepseek-secret" for request in requests[:2])
        )
        self.assertIn("dashscope.aliyuncs.com", requests[2].full_url)
        self.assertEqual(requests[2].get_header("Authorization"), "Bearer qwen-secret")


class LLMAdapterRouterTests(unittest.TestCase):
    def test_status_activate_and_clear_share_one_registry(self):
        registry = LLMAdapterRegistry()
        verifier = Mock()
        router = create_llm_adapter_router(registry, verifier)
        status_endpoint = next(
            route.endpoint for route in router.routes if route.path.endswith("/status")
        )
        activate_endpoint = next(
            route.endpoint for route in router.routes if route.path.endswith("/activate")
        )
        clear_endpoint = next(
            route.endpoint for route in router.routes if route.path.endswith("/active")
        )
        activate_saved_endpoint = next(
            route.endpoint
            for route in router.routes
            if route.path.endswith("/activate-saved/{provider}")
        )
        delete_endpoint = next(
            route.endpoint
            for route in router.routes
            if route.path.endswith("/credentials/{provider}")
        )
        request = Request({"type": "http", "headers": []})

        initial = status_endpoint(request)
        activated = activate_endpoint(
            ActivateAdapterRequest(provider="qwen", api_key="request-secret"),
            request,
        )
        cleared = clear_endpoint(request)
        restored = activate_saved_endpoint(AdapterKind.QWEN, request)
        deleted = delete_endpoint(AdapterKind.QWEN, request)

        self.assertEqual(len(initial.adapters), 3)
        self.assertEqual(activated.active.provider, AdapterKind.QWEN)
        self.assertIsNone(cleared.active)
        self.assertEqual(restored.active.provider, AdapterKind.QWEN)
        self.assertEqual(verifier.probe.call_count, 2)
        self.assertIsNone(deleted.active)
        self.assertFalse(
            next(
                adapter
                for adapter in deleted.adapters
                if adapter.provider is AdapterKind.QWEN
            ).configured
        )

    def test_failed_probe_preserves_previously_active_adapter(self):
        registry = LLMAdapterRegistry()
        registry.activate(AdapterKind.DEEPSEEK, "deepseek-secret")
        verifier = Mock()
        verifier.probe.side_effect = AdapterServiceError("连接验证失败")
        router = create_llm_adapter_router(registry, verifier)
        activate_endpoint = next(
            route.endpoint for route in router.routes if route.path.endswith("/activate")
        )

        with self.assertRaises(HTTPException) as raised:
            activate_endpoint(
                ActivateAdapterRequest(provider="qwen", api_key="request-secret"),
                Request({"type": "http", "headers": []}),
            )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(registry.snapshot().definition.kind, AdapterKind.DEEPSEEK)


class ActivateAdapterRequestTests(unittest.TestCase):
    def test_key_prefix_does_not_override_console_base_url(self):
        request = ActivateAdapterRequest(
            provider="qwen",
            api_key="sk-ws-example-secret",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        self.assertEqual(
            request.base_url,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    def test_workspace_endpoint_is_normalized(self):
        request = ActivateAdapterRequest(
            provider="qwen",
            api_key="sk-ws-example-secret",
            base_url=(
                "https://WS-EXAMPLE.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1/"
            ),
        )

        self.assertEqual(
            request.base_url,
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )

    def test_qwen_endpoint_rejects_untrusted_host(self):
        with self.assertRaisesRegex(ValidationError, "阿里云百炼提供"):
            ActivateAdapterRequest(
                provider="qwen",
                api_key="request-secret",
                base_url="https://example.com/compatible-mode/v1",
            )

    def test_other_providers_reject_custom_endpoint(self):
        with self.assertRaisesRegex(ValidationError, "只有千问"):
            ActivateAdapterRequest(
                provider="deepseek",
                api_key="request-secret",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )

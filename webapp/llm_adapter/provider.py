from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import ssl
import time
from typing import Any, ContextManager, Iterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from webapp.llm_adapter.errors import (
    AdapterNotConfiguredError,
    AdapterResponseError,
    AdapterServiceError,
)
from webapp.llm_adapter.catalog import AdapterKind
from webapp.llm_adapter.registry import AdapterCredentials, LLMAdapterRegistry


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatProvider(Protocol):
    @property
    def ready(self) -> bool: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_label(self) -> str: ...

    def session(self) -> ContextManager[None]: ...

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, str] | None = None,
        temperature: float = 0.4,
    ) -> dict[str, Any]: ...


class OpenAICompatibleProvider:
    """Calls the active adapter while exposing one chat-style interface."""

    def __init__(self, registry: LLMAdapterRegistry, timeout_seconds: float = 90) -> None:
        self._registry = registry
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._session_credentials: ContextVar[AdapterCredentials | None] = ContextVar(
            "llm_adapter_session_credentials", default=None
        )

    def _active_credentials(self) -> AdapterCredentials | None:
        return self._session_credentials.get() or self._registry.snapshot()

    @property
    def ready(self) -> bool:
        return self._active_credentials() is not None

    @property
    def model(self) -> str:
        active = self._active_credentials()
        return active.definition.model if active else ""

    @property
    def provider_label(self) -> str:
        active = self._active_credentials()
        return active.definition.label if active else ""

    @contextmanager
    def session(self) -> Iterator[None]:
        with self._registry.lease() as active:
            if not active:
                raise AdapterNotConfiguredError(
                    "尚未启用 LLM 适配器，请先在左侧“LLM 适配器”中选择模型并填写 API Key。"
                )
            token = self._session_credentials.set(active)
            try:
                yield
            finally:
                self._session_credentials.reset(token)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, str] | None = None,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        active = self._active_credentials()
        if not active:
            raise AdapterNotConfiguredError(
                "尚未启用 LLM 适配器，请先在左侧“LLM 适配器”中选择模型并填写 API Key。"
            )

        definition = active.definition
        payload: dict[str, Any] = {
            "model": definition.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = response_format

        return self._request_message(active, payload, self._timeout_seconds)

    def probe(self, credentials: AdapterCredentials) -> None:
        """Validate credentials before replacing the currently active adapter."""
        probe_timeout = (
            30 if credentials.definition.kind is AdapterKind.DEEPSEEK else 20
        )
        self._request_message(
            credentials,
            {
                "model": credentials.definition.model,
                "messages": [{"role": "user", "content": "回复 OK"}],
                "temperature": 0,
                "max_tokens": 1,
            },
            min(self._timeout_seconds, probe_timeout),
        )

    def _request_message(
        self,
        active: AdapterCredentials,
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        document = self._post_json(
            active,
            "chat/completions",
            payload,
            timeout_seconds,
        )
        try:
            message = document["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterResponseError(
                f"{active.definition.label} 返回了不符合 chat-completions 契约的响应"
            ) from exc
        if not isinstance(message, dict):
            raise AdapterResponseError(
                f"{active.definition.label} 返回的 message 不是对象"
            )
        return message

    def _post_json(
        self,
        active: AdapterCredentials,
        path: str,
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        definition = active.definition
        request = Request(
            f"{definition.base_url.rstrip('/')}/{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {active.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        max_attempts = 2 if definition.kind is AdapterKind.DEEPSEEK else 1
        transient_http_codes = {408, 425, 429, 500, 502, 503, 504}
        for attempt in range(max_attempts):
            try:
                opener = build_opener(
                    _NoRedirectHandler(), HTTPSHandler(context=self._ssl_context)
                )
                with opener.open(request, timeout=timeout_seconds) as response:
                    raw = response.read(2_000_001)
                break
            except HTTPError as exc:
                if exc.code in transient_http_codes and attempt + 1 < max_attempts:
                    time.sleep(attempt + 1)
                    continue
                detail = exc.read(1000).decode("utf-8", errors="replace").strip()
                suffix = f"：{detail[:500]}" if detail else ""
                raise AdapterServiceError(
                    f"{definition.label} 返回 HTTP {exc.code}{suffix}"
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < max_attempts:
                    time.sleep(attempt + 1)
                    continue
                raise AdapterServiceError(
                    f"无法连接 {definition.label}（已自动重试）：{exc}"
                ) from exc

        if len(raw) > 2_000_000:
            raise AdapterResponseError(f"{definition.label} 响应超过 2 MB 限制")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterResponseError(f"{definition.label} 返回了无效 JSON") from exc
        if not isinstance(document, dict):
            raise AdapterResponseError(f"{definition.label} 返回的响应不是对象")
        return document

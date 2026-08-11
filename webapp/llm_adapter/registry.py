from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Iterator

from webapp.llm_adapter.catalog import (
    ADAPTER_CATALOG,
    AdapterDefinition,
    AdapterKind,
    resolve_adapter_definition,
)
from webapp.llm_adapter.contracts import ActiveAdapter, AdapterOption, AdapterStatus
from webapp.llm_adapter.credential_store import (
    AdapterCredentialStore,
    StoredAdapterCredential,
    StoredAdapterState,
)
from webapp.llm_adapter.errors import AdapterNotConfiguredError, AdapterStorageError


@dataclass(frozen=True, slots=True)
class AdapterCredentials:
    definition: AdapterDefinition
    api_key: str


class LLMAdapterRegistry:
    """Keep saved provider credentials while exposing only one active route."""

    def __init__(self, credential_store: AdapterCredentialStore | None = None) -> None:
        self._lock = RLock()
        self._credential_store = credential_store
        self._saved: dict[AdapterKind, AdapterCredentials] = {}
        self._active: AdapterCredentials | None = None
        self._restore()

    def _restore(self) -> None:
        if self._credential_store is None:
            return
        state = self._credential_store.load()
        try:
            self._saved = {
                provider: self.prepare(
                    provider,
                    stored.api_key,
                    stored.base_url,
                )
                for provider, stored in state.credentials.items()
            }
        except ValueError as exc:
            raise AdapterStorageError("本地 LLM API Key 配置包含无效地址") from exc
        self._active = self._saved.get(state.active_provider)

    def _persist(
        self,
        saved: dict[AdapterKind, AdapterCredentials],
        active: AdapterCredentials | None,
    ) -> None:
        if self._credential_store is None:
            return
        self._credential_store.save(
            StoredAdapterState(
                credentials={
                    provider: StoredAdapterCredential(
                        credentials.api_key,
                        (
                            credentials.definition.base_url
                            if provider is AdapterKind.QWEN
                            else None
                        ),
                    )
                    for provider, credentials in saved.items()
                },
                active_provider=active.definition.kind if active else None,
            )
        )

    def prepare(
        self, provider: AdapterKind, api_key: str, base_url: str | None = None
    ) -> AdapterCredentials:
        return AdapterCredentials(
            definition=resolve_adapter_definition(provider, base_url), api_key=api_key
        )

    def activate_credentials(self, credentials: AdapterCredentials) -> AdapterStatus:
        with self._lock:
            saved = dict(self._saved)
            saved[credentials.definition.kind] = credentials
            self._persist(saved, credentials)
            self._saved = saved
            self._active = credentials
        return self.status()

    def saved_credentials(self, provider: AdapterKind) -> AdapterCredentials:
        with self._lock:
            credentials = self._saved.get(provider)
        if credentials is None:
            raise AdapterNotConfiguredError(
                f"{ADAPTER_CATALOG[provider].label} 尚未保存 API Key"
            )
        return credentials

    def activate_saved_credentials(
        self, credentials: AdapterCredentials
    ) -> AdapterStatus:
        provider = credentials.definition.kind
        with self._lock:
            current = self._saved.get(provider)
            if current != credentials:
                raise AdapterNotConfiguredError(
                    f"{ADAPTER_CATALOG[provider].label} 保存的 API Key 已发生变化，请重试"
                )
            self._persist(self._saved, current)
            self._active = current
        return self.status()

    def activate(
        self, provider: AdapterKind, api_key: str, base_url: str | None = None
    ) -> AdapterStatus:
        return self.activate_credentials(self.prepare(provider, api_key, base_url))

    def clear(self) -> AdapterStatus:
        with self._lock:
            self._persist(self._saved, None)
            self._active = None
        return self.status()

    def delete_credentials(self, provider: AdapterKind) -> AdapterStatus:
        with self._lock:
            saved = dict(self._saved)
            saved.pop(provider, None)
            active = self._active
            if active and active.definition.kind is provider:
                active = None
            self._persist(saved, active)
            self._saved = saved
            self._active = active
        return self.status()

    def snapshot(self) -> AdapterCredentials | None:
        with self._lock:
            return self._active

    @contextmanager
    def lease(self) -> Iterator[AdapterCredentials | None]:
        """Return an immutable credential snapshot without serializing generations."""
        yield self.snapshot()

    def status(self) -> AdapterStatus:
        with self._lock:
            active = self._active
            saved = dict(self._saved)
        return AdapterStatus(
            adapters=[
                AdapterOption(
                    provider=definition.kind,
                    label=definition.label,
                    description=definition.description,
                    model=definition.model,
                    model_label=definition.model_label,
                    endpoint=(
                        saved[definition.kind].definition.base_url
                        if definition.kind in saved
                        else definition.base_url
                    ),
                    key_hint=definition.key_hint,
                    configured=definition.kind in saved,
                )
                for definition in ADAPTER_CATALOG.values()
            ],
            active=(
                ActiveAdapter(
                    provider=active.definition.kind,
                    label=active.definition.label,
                    model=active.definition.model,
                    model_label=active.definition.model_label,
                    endpoint=active.definition.base_url,
                )
                if active
                else None
            ),
        )

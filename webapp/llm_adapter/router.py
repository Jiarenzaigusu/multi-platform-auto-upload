from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request

from webapp.llm_adapter.catalog import AdapterKind
from webapp.llm_adapter.contracts import ActivateAdapterRequest, AdapterStatus
from webapp.llm_adapter.errors import LLMAdapterError
from webapp.llm_adapter.provider import OpenAICompatibleProvider
from webapp.llm_adapter.registry import LLMAdapterRegistry


def create_llm_adapter_router(
    registry: LLMAdapterRegistry | Callable[[Request], LLMAdapterRegistry],
    verifier: OpenAICompatibleProvider | None = None,
    write_authorizer: Callable[[Request], None] | None = None,
) -> APIRouter:
    """Create static or request-scoped adapter routes from one implementation."""
    router = APIRouter(prefix="/api/llm-adapter", tags=["llm-adapter"])

    def resolve(request: Request) -> LLMAdapterRegistry:
        return registry(request) if callable(registry) else registry

    def resolve_verifier(selected: LLMAdapterRegistry) -> OpenAICompatibleProvider:
        if verifier is not None:
            return verifier
        return OpenAICompatibleProvider(selected)

    @router.get("/status", response_model=AdapterStatus)
    def get_status(request: Request) -> AdapterStatus:
        return resolve(request).status()

    @router.post("/activate", response_model=AdapterStatus)
    def activate_adapter(
        payload: ActivateAdapterRequest, request: Request
    ) -> AdapterStatus:
        if write_authorizer is not None:
            write_authorizer(request)
        selected = resolve(request)
        try:
            credentials = selected.prepare(
                payload.provider, payload.api_key, payload.base_url
            )
            resolve_verifier(selected).probe(credentials)
            return selected.activate_credentials(credentials)
        except LLMAdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/activate-saved/{provider}", response_model=AdapterStatus)
    def activate_saved_adapter(provider: AdapterKind, request: Request) -> AdapterStatus:
        if write_authorizer is not None:
            write_authorizer(request)
        selected = resolve(request)
        try:
            credentials = selected.saved_credentials(provider)
            resolve_verifier(selected).probe(credentials)
            return selected.activate_saved_credentials(credentials)
        except LLMAdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.delete("/active", response_model=AdapterStatus)
    def clear_active_adapter(request: Request) -> AdapterStatus:
        if write_authorizer is not None:
            write_authorizer(request)
        try:
            return resolve(request).clear()
        except LLMAdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.delete("/credentials/{provider}", response_model=AdapterStatus)
    def delete_adapter_credentials(provider: AdapterKind, request: Request) -> AdapterStatus:
        if write_authorizer is not None:
            write_authorizer(request)
        try:
            return resolve(request).delete_credentials(provider)
        except LLMAdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return router

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request

from webapp.ai_copy.contracts import (
    FESTIVAL_SUGGESTIONS,
    GenerateCopyRequest,
    GenerateCopyResponse,
    ProductReference,
    ProductReferenceRequest,
    SCENE_LABELS,
    STYLE_LABELS,
)
from webapp.ai_copy.errors import AiCopyError
from webapp.ai_copy.service import AiCopyService
from webapp.llm_adapter.errors import LLMAdapterError


def create_ai_copy_router(
    service: AiCopyService | Callable[[Request], AiCopyService]
) -> APIRouter:
    """Create AI routes that support a request-scoped user workspace."""
    router = APIRouter(prefix="/api/ai-copy", tags=["ai-copy"])

    def resolve(request: Request) -> AiCopyService:
        return service(request) if callable(service) else service

    @router.get("/options")
    def get_options(request: Request) -> dict:
        selected = resolve(request)
        return {
            "styles": [
                {"value": value.value, "label": label}
                for value, label in STYLE_LABELS.items()
            ],
            "scenes": [
                {"value": value.value, "label": label}
                for value, label in SCENE_LABELS.items()
            ],
            "festivals": list(FESTIVAL_SUGGESTIONS),
            "llm": {
                "ready": selected.llm_ready,
                "model": selected.model,
                "provider": selected.provider_label,
            },
        }

    @router.post("/product-reference", response_model=ProductReference)
    async def inspect_product(
        payload: ProductReferenceRequest, request: Request
    ) -> ProductReference:
        try:
            return await asyncio.to_thread(resolve(request).inspect_product, payload)
        except (AiCopyError, LLMAdapterError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.post("/generate", response_model=GenerateCopyResponse)
    async def generate_copy(
        payload: GenerateCopyRequest, request: Request
    ) -> GenerateCopyResponse:
        try:
            return await asyncio.to_thread(resolve(request).generate, payload)
        except (AiCopyError, LLMAdapterError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return router

__all__ = ["AiCopyService", "build_ai_copy_service", "create_ai_copy_router"]


def __getattr__(name: str):
    if name == "create_ai_copy_router":
        from webapp.ai_copy.router import create_ai_copy_router

        return create_ai_copy_router
    if name in {"AiCopyService", "build_ai_copy_service"}:
        from webapp.ai_copy.service import AiCopyService, build_ai_copy_service

        return {
            "AiCopyService": AiCopyService,
            "build_ai_copy_service": build_ai_copy_service,
        }[name]
    raise AttributeError(name)

class AiCopyError(Exception):
    """Base error for the isolated AI copywriting feature."""

    status_code = 500


class ProductLookupError(AiCopyError):
    status_code = 502


class LLMResponseError(AiCopyError):
    status_code = 502

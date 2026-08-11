class LLMAdapterError(Exception):
    """Base error for the isolated LLM adapter feature."""

    status_code = 500


class AdapterNotConfiguredError(LLMAdapterError):
    status_code = 503


class AdapterServiceError(LLMAdapterError):
    status_code = 502


class AdapterResponseError(LLMAdapterError):
    status_code = 502


class AdapterStorageError(LLMAdapterError):
    status_code = 500

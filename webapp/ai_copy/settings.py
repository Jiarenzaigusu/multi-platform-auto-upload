from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AiCopySettings:
    product_timeout_seconds: float = 20
    max_product_page_bytes: int = 1_500_000
    product_cache_seconds: float = 600
    product_stale_cache_seconds: float = 3_600
    jd_request_attempts: int = 5
    jd_retry_base_seconds: float = 0.15
    tmall_account_attempts: int = 2

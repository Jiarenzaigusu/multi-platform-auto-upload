from __future__ import annotations

import re
import time
from typing import Protocol

from patchright.sync_api import Error as PatchrightError
from patchright.sync_api import TimeoutError as PatchrightTimeoutError
from patchright.sync_api import sync_playwright

from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.public_http import FetchedPage


JD_MOBILE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Mobile/15E148"
    ),
}


class JdPageFetcher(Protocol):
    def get(
        self, product_url: str, *, headers: dict[str, str] | None = None
    ) -> FetchedPage: ...


class _TransientJdRequestError(ProductLookupError):
    pass


class PatchrightJdPageFetcher:
    """Uses Patchright's request stack for JD's TLS and anti-bot compatibility."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        max_attempts: int = 5,
        retry_base_seconds: float = 0.15,
    ) -> None:
        self._timeout_ms = timeout_seconds * 1000
        self._max_bytes = max_bytes
        self._max_attempts = max(1, max_attempts)
        self._retry_base_seconds = max(0, retry_base_seconds)

    def get(
        self, product_url: str, *, headers: dict[str, str] | None = None
    ) -> FetchedPage:
        last_error: _TransientJdRequestError | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._get_once(product_url, headers=headers)
            except _TransientJdRequestError as exc:
                last_error = exc
                if attempt + 1 < self._max_attempts:
                    time.sleep(self._retry_base_seconds * (2**attempt))
        raise ProductLookupError(str(last_error)) from last_error

    def _get_once(
        self, product_url: str, *, headers: dict[str, str] | None = None
    ) -> FetchedPage:
        request_headers = dict(headers or {})
        user_agent = request_headers.pop("User-Agent", JD_MOBILE_HEADERS["User-Agent"])
        try:
            with sync_playwright() as playwright:
                request_context = playwright.request.new_context(
                    user_agent=user_agent,
                    extra_http_headers=request_headers,
                )
                try:
                    response = request_context.get(
                        product_url,
                        timeout=self._timeout_ms,
                        max_redirects=0,
                    )
                    if response.status in {301, 302, 303, 307, 308}:
                        raise _TransientJdRequestError(
                            "京东商品页面限制了当前读取请求"
                        )
                    if response.status >= 500:
                        raise _TransientJdRequestError(
                            f"京东商品页面返回 HTTP {response.status}"
                        )
                    if response.status >= 400:
                        raise ProductLookupError(
                            f"京东商品页面返回 HTTP {response.status}"
                        )
                    content = response.body()
                    if len(content) > self._max_bytes:
                        raise ProductLookupError("京东商品页面超过读取大小限制")
                    content_type_header = response.headers.get("content-type", "")
                    content_type = content_type_header.split(";", 1)[0].strip().lower()
                    charset_match = re.search(
                        r"charset=([^;\s]+)", content_type_header, re.IGNORECASE
                    )
                    charset = (
                        charset_match.group(1).strip('"\'')
                        if charset_match
                        else "utf-8"
                    )
                    return FetchedPage(
                        content=content,
                        content_type=content_type,
                        charset=charset,
                        final_url=response.url,
                    )
                finally:
                    request_context.dispose()
        except ProductLookupError:
            raise
        except PatchrightTimeoutError as exc:
            raise _TransientJdRequestError("读取京东商品页面超时") from exc
        except PatchrightError as exc:
            raise _TransientJdRequestError(f"无法读取京东商品页面：{exc}") from exc

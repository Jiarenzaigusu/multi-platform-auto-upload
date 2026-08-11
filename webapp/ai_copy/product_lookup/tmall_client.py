from __future__ import annotations

import json
from collections.abc import Coroutine, Sequence
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import parse_qs, urlsplit

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeoutError

from webapp.ai_copy.errors import ProductLookupError
from webapp.ai_copy.product_lookup.public_http import FetchedPage

T = TypeVar("T")


class TmallPageFetcher(Protocol):
    def get(self, product_url: str) -> FetchedPage: ...


class TmallBrowserRuntime(Protocol):
    def run(self, coroutine: Coroutine[Any, Any, T]) -> T: ...

    def tmall_sessions(self) -> Any: ...


class TmallStorageStateProvider(Protocol):
    def candidates(self) -> Sequence[Path]: ...


class DirectoryTmallStorageStateProvider:
    """Find recent usable Tmall account states without exposing account data."""

    def __init__(self, cookie_dir: Path, *, max_candidates: int = 2) -> None:
        self._cookie_dir = cookie_dir
        self._max_candidates = max(1, max_candidates)

    def candidates(self) -> Sequence[Path]:
        try:
            paths = sorted(
                self._cookie_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return ()

        usable: list[Path] = []
        for path in paths:
            if self._is_usable(path):
                usable.append(path.resolve())
                if len(usable) >= self._max_candidates:
                    break
        return tuple(usable)

    @staticmethod
    def _is_usable(path: Path) -> bool:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies, list):
            return False
        return any(
            isinstance(cookie, dict)
            and str(cookie.get("domain", "")).lower().endswith(
                ("taobao.com", "tmall.com")
            )
            for cookie in cookies
        )


def _looks_like_tmall_product(
    product_url: str, final_url: str, page_text: str
) -> bool:
    final_host = (urlsplit(final_url).hostname or "").lower()
    if final_host.endswith("login.taobao.com") or '"action":"login"' in page_text:
        return False
    item_id = parse_qs(urlsplit(product_url).query).get("id", [""])[0]
    return bool(
        item_id
        and item_id in page_text
        and '"loaderData"' in page_text
        and '"skuCore"' in page_text
    )


class BrowserRuntimeTmallPageFetcher:
    """Load a Tmall product through the app's reusable authenticated browser pool."""

    def __init__(
        self,
        runtime: TmallBrowserRuntime,
        storage_states: TmallStorageStateProvider,
        *,
        timeout_seconds: float,
        max_bytes: int,
        browser_slots: AbstractContextManager[Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._storage_states = storage_states
        self._timeout_ms = timeout_seconds * 1000
        self._max_bytes = max_bytes
        self._browser_slots = browser_slots

    def get(self, product_url: str) -> FetchedPage:
        slot = self._browser_slots or nullcontext()
        with slot:
            return self._runtime.run(self.get_async(product_url))

    async def get_async(self, product_url: str) -> FetchedPage:
        states = self._storage_states.candidates()
        if not states:
            raise ProductLookupError(
                "未找到可用的天猫登录状态，请先在账号管理中登录天猫账号"
            )

        last_error = "现有天猫登录状态无法读取该商品"
        for account_file in states:
            page = None
            try:
                session_pool = self._runtime.tmall_sessions()
                async with session_pool.lease(
                    account_file,
                    headless=True,
                    preserve_existing_mode=True,
                ) as session:
                    context = await session.ensure_open()
                    page = await context.new_page()
                    response = await page.goto(
                        product_url,
                        wait_until="domcontentloaded",
                        timeout=self._timeout_ms,
                    )
                    if response is None:
                        last_error = "天猫商品页面没有返回响应"
                        continue
                    if response.status >= 400:
                        last_error = f"天猫商品页面返回 HTTP {response.status}"
                        continue

                    # The product payload is server-rendered, but a short settle period
                    # lets Tmall finish a client-side redirect when login has expired.
                    await page.wait_for_timeout(500)
                    page_text = await page.content()
                    content = page_text.encode("utf-8")
                    if len(content) > self._max_bytes:
                        last_error = "天猫商品页面超过读取大小限制"
                        continue
                    if not _looks_like_tmall_product(
                        product_url, page.url, page_text
                    ):
                        last_error = "天猫登录状态已失效或页面触发了访问验证"
                        continue

                    session.mark_authenticated(True)
                    return FetchedPage(
                        content=content,
                        content_type="text/html",
                        charset="utf-8",
                        final_url=page.url,
                    )
            except PatchrightTimeoutError:
                last_error = "读取天猫商品页面超时"
            except PatchrightError as exc:
                last_error = f"无法读取天猫商品页面：{exc}"
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except PatchrightError:
                        pass

        raise ProductLookupError(last_error)


__all__ = [
    "BrowserRuntimeTmallPageFetcher",
    "DirectoryTmallStorageStateProvider",
    "TmallPageFetcher",
]

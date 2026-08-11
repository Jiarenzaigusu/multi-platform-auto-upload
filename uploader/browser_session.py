from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from patchright.async_api import Browser, BrowserContext, Playwright, async_playwright

from utils.base_social_media import set_init_script
from utils.config import LOCAL_EDGE_PATH


async def launch_browser(playwright: Playwright, headless: bool) -> Browser:
    if LOCAL_EDGE_PATH:
        return await playwright.chromium.launch(
            headless=headless,
            executable_path=LOCAL_EDGE_PATH,
        )
    return await playwright.chromium.launch(headless=headless, channel="msedge")


class BrowserSession:
    """A reusable Edge process and context for one platform account."""

    def __init__(
        self,
        playwright: Playwright,
        account_file: str | Path,
        *,
        headless: bool,
        logger: Any,
        platform_label: str,
        viewport: dict[str, int],
        launcher: Callable[[Playwright, bool], Awaitable[Browser]] = launch_browser,
    ) -> None:
        self.playwright = playwright
        self.account_file = Path(account_file).resolve()
        self.headless = headless
        self.logger = logger
        self.platform_label = platform_label
        self.viewport = viewport
        self.launcher = launcher
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.busy = 0
        self.last_used_at = time.monotonic()
        self.last_auth_check_at = 0.0
        self.last_auth_result = False

    @property
    def key(self) -> str:
        return str(self.account_file)

    @property
    def is_connected(self) -> bool:
        return bool(self.browser and self.browser.is_connected() and self.context)

    def touch(self) -> None:
        self.last_used_at = time.monotonic()

    def auth_is_fresh(self, max_age_seconds: float) -> bool:
        return (
            self.last_auth_result
            and self.last_auth_check_at > 0
            and time.monotonic() - self.last_auth_check_at <= max_age_seconds
        )

    def mark_authenticated(self, authenticated: bool) -> None:
        self.last_auth_result = authenticated
        self.last_auth_check_at = time.monotonic()
        self.touch()

    async def ensure_open(self) -> BrowserContext:
        if self.is_connected:
            self.touch()
            assert self.context is not None
            return self.context

        await self.close()
        self.browser = await self.launcher(self.playwright, self.headless)
        context_options: dict[str, object] = {"viewport": self.viewport}
        if self.account_file.is_file():
            context_options["storage_state"] = str(self.account_file)
        try:
            self.context = await self.browser.new_context(**context_options)
        except Exception:
            if "storage_state" not in context_options:
                raise
            self.logger.warning("cookie 状态文件无法载入，将使用空白会话等待重新登录")
            self.context = await self.browser.new_context(viewport=self.viewport)
        self.context = await set_init_script(self.context)
        self.touch()
        return self.context

    async def save_storage_state(self) -> None:
        if not self.context:
            return
        await self.context.storage_state(path=str(self.account_file))
        try:
            self.account_file.chmod(0o600)
        except FileNotFoundError:
            pass
        self.touch()

    async def close(self) -> None:
        context, browser = self.context, self.browser
        self.context = None
        self.browser = None
        self.last_auth_check_at = 0.0
        self.last_auth_result = False
        if context:
            with suppress(Exception):
                await context.close()
        if browser:
            with suppress(Exception):
                await browser.close()


class BrowserSessionPool:
    """Keep one reusable browser session per account with bounded idle retention."""

    session_class = BrowserSession

    def __init__(
        self,
        *,
        logger: Any,
        platform_label: str,
        viewport: dict[str, int],
        idle_timeout_seconds: float = 20 * 60,
        max_sessions: int = 2,
        playwright_starter: Callable[[], Awaitable[Playwright]] | None = None,
        launcher: Callable[[Playwright, bool], Awaitable[Browser]] = launch_browser,
    ) -> None:
        self.logger = logger
        self.platform_label = platform_label
        self.viewport = viewport
        self.idle_timeout_seconds = max(0.0, idle_timeout_seconds)
        self.max_sessions = max(1, max_sessions)
        self._playwright_starter = playwright_starter or self._start_playwright
        self._launcher = launcher
        self._playwright: Playwright | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._closed = False

    @staticmethod
    async def _start_playwright() -> Playwright:
        return await async_playwright().start()

    @staticmethod
    def _key(account_file: str | Path) -> str:
        return str(Path(account_file).resolve())

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def _ensure_playwright(self) -> Playwright:
        if self._closed:
            raise RuntimeError(f"{self.platform_label}浏览器会话池已经关闭")
        if self._playwright is None:
            self._playwright = await self._playwright_starter()
        return self._playwright

    def _ensure_reaper(self) -> None:
        if self._closed:
            raise RuntimeError(f"{self.platform_label}浏览器会话池已经关闭")
        if self.idle_timeout_seconds <= 0 or self._reaper_task is not None:
            return
        self._reaper_task = asyncio.create_task(
            self._reap_loop(),
            name=f"{self.platform_label.lower()}-session-reaper",
        )

    async def _reap_loop(self) -> None:
        interval = min(60.0, max(1.0, self.idle_timeout_seconds / 2))
        try:
            while True:
                await asyncio.sleep(interval)
                await self.reap_idle()
        except asyncio.CancelledError:
            return

    async def _discard_locked(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session:
            if session.last_auth_result:
                with suppress(Exception):
                    await session.save_storage_state()
            await session.close()

    async def _evict_lru_locked(self) -> None:
        idle_sessions = [session for session in self._sessions.values() if not session.busy]
        if not idle_sessions:
            return
        oldest = min(idle_sessions, key=lambda session: session.last_used_at)
        await self._discard_locked(oldest.key)

    @asynccontextmanager
    async def lease(
        self,
        account_file: str | Path,
        *,
        headless: bool,
        preserve_existing_mode: bool = False,
    ) -> AsyncIterator[BrowserSession]:
        key = self._key(account_file)
        async with self._lock:
            self._ensure_reaper()
            session = self._sessions.get(key)
            requested_headless = session.headless if session and preserve_existing_mode else headless
            if session and (session.headless != requested_headless or not session.is_connected):
                if session.busy:
                    raise RuntimeError("同一账号的浏览器会话仍在使用中，不能切换显示模式")
                reason = "显示模式变化" if session.headless != requested_headless else "浏览器连接已断开"
                self.logger.info(
                    f"准备重建{self.platform_label}浏览器会话：{Path(key).stem}（{reason}）"
                )
                await self._discard_locked(key)
                session = None
            if session is None:
                if len(self._sessions) >= self.max_sessions:
                    await self._evict_lru_locked()
                playwright = await self._ensure_playwright()
                session = self.session_class(
                    playwright,
                    key,
                    headless=requested_headless,
                    logger=self.logger,
                    platform_label=self.platform_label,
                    viewport=self.viewport,
                    launcher=self._launcher,
                )
                try:
                    await session.ensure_open()
                except Exception:
                    await session.close()
                    raise
                self._sessions[key] = session
                self.logger.info(f"{self.platform_label}浏览器冷启动完成：{Path(key).stem}")
            else:
                self.logger.info(f"复用{self.platform_label}浏览器会话：{Path(key).stem}")
            session.busy += 1
            session.touch()

        try:
            yield session
        finally:
            async with self._lock:
                current = self._sessions.get(key)
                if current is session:
                    session.busy = max(0, session.busy - 1)
                    session.touch()

    async def reap_idle(self) -> None:
        if self.idle_timeout_seconds <= 0:
            return
        cutoff = time.monotonic() - self.idle_timeout_seconds
        async with self._lock:
            stale_keys = [
                key
                for key, session in self._sessions.items()
                if not session.busy and session.last_used_at <= cutoff
            ]
            for key in stale_keys:
                self.logger.info(
                    f"{self.platform_label}浏览器会话空闲超时，正在关闭：{Path(key).stem}"
                )
                await self._discard_locked(key)

    async def close_account(self, account_file: str | Path) -> None:
        key = self._key(account_file)
        async with self._lock:
            session = self._sessions.get(key)
            if session and session.busy:
                raise RuntimeError("账号浏览器会话仍在执行任务，暂时不能关闭")
            await self._discard_locked(key)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reaper = self._reaper_task
        self._reaper_task = None
        if reaper:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
        async with self._lock:
            for key in list(self._sessions):
                await self._discard_locked(key)
        if self._playwright:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

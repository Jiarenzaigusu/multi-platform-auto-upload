from __future__ import annotations

from typing import Awaitable, Callable

from patchright.async_api import Browser, Playwright

from uploader.browser_session import BrowserSession, BrowserSessionPool, launch_browser
from utils.log import jd_logger


class JdBrowserSession(BrowserSession):
    pass


class JdSessionPool(BrowserSessionPool):
    session_class = JdBrowserSession

    def __init__(
        self,
        *,
        logger=jd_logger,
        idle_timeout_seconds: float = 20 * 60,
        max_sessions: int = 2,
        playwright_starter: Callable[[], Awaitable[Playwright]] | None = None,
        launcher: Callable[[Playwright, bool], Awaitable[Browser]] = launch_browser,
    ) -> None:
        super().__init__(
            logger=logger,
            platform_label="京东",
            viewport={"width": 1440, "height": 900},
            idle_timeout_seconds=idle_timeout_seconds,
            max_sessions=max_sessions,
            playwright_starter=playwright_starter,
            launcher=launcher,
        )

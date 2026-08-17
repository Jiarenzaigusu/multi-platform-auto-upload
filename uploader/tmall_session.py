# -*- coding: utf-8 -*-
"""Shared browser-session pool for all Tmall Guanghe content types."""
from __future__ import annotations

from typing import Awaitable, Callable

from patchright.async_api import Browser, Playwright

from uploader.browser_session import BrowserSession, BrowserSessionPool, launch_browser
from utils.log import tmall_logger


class TmallBrowserSession(BrowserSession):
    """Browser context for one authenticated Tmall account."""


class TmallSessionPool(BrowserSessionPool):
    """Pool shared by Tmall video and article publishers for one account."""

    session_class = TmallBrowserSession

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = 0,
        max_sessions: int = 2,
        playwright_starter: Callable[[], Awaitable[Playwright]] | None = None,
        launcher: Callable[[Playwright, bool], Awaitable[Browser]] = launch_browser,
    ) -> None:
        super().__init__(
            logger=tmall_logger,
            platform_label="天猫",
            viewport={"width": 1280, "height": 900},
            idle_timeout_seconds=idle_timeout_seconds,
            max_sessions=max_sessions,
            playwright_starter=playwright_starter,
            launcher=launcher,
        )

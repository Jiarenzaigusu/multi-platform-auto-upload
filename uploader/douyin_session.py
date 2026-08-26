# -*- coding: utf-8 -*-
"""Shared browser-session pool for all Douyin content types."""
from __future__ import annotations

from typing import Awaitable, Callable

from patchright.async_api import Browser, Playwright

from uploader.browser_session import (
    BrowserSession,
    BrowserSessionPool,
    launch_chrome_browser,
)
from utils.log import douyin_logger


class DouyinBrowserSession(BrowserSession):
    """Browser context for one authenticated Douyin account."""

    def context_options(self) -> dict[str, object]:
        return {"permissions": ["geolocation"]}


class DouyinSessionPool(BrowserSessionPool):
    """Pool shared by Douyin video and article publishers for one account."""

    session_class = DouyinBrowserSession

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = 0,
        max_sessions: int = 2,
        playwright_starter: Callable[[], Awaitable[Playwright]] | None = None,
        launcher: Callable[[Playwright, bool], Awaitable[Browser]] = launch_chrome_browser,
    ) -> None:
        super().__init__(
            logger=douyin_logger,
            platform_label="抖音",
            viewport={"width": 1280, "height": 900},
            idle_timeout_seconds=idle_timeout_seconds,
            max_sessions=max_sessions,
            playwright_starter=playwright_starter,
            launcher=launcher,
        )

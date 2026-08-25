from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import CancelledError as FutureCancelledError, Future
import threading
from typing import Any, TypeVar

from utils.log import user_platform_logger


T = TypeVar("T")


class BrowserRuntime:
    """Own the long-lived event loop used by reusable Playwright objects."""

    def __init__(
        self,
        *,
        user_id: str,
        idle_timeout_seconds: float = 0,
        max_sessions: int = 2,
    ) -> None:
        self.user_id = user_id
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_sessions = max_sessions
        self._guard = threading.RLock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tmall_sessions = None
        self._jd_sessions = None

    @property
    def started(self) -> bool:
        with self._guard:
            return bool(self._thread and self._thread.is_alive() and self._loop)

    def start(self) -> None:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="mpau-browser-runtime",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("浏览器运行时启动超时")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._guard:
            self._loop = loop
            self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def is_current_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        future = self.submit(coroutine)
        try:
            return future.result()
        except FutureCancelledError as exc:
            raise asyncio.CancelledError from exc

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        """Schedule browser work while allowing a local agent to cancel the future."""
        self.start()
        with self._guard:
            loop = self._loop
        if loop is None:
            coroutine.close()
            raise RuntimeError("浏览器运行时尚未就绪")
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def tmall_sessions(self):
        if not self.is_current_loop():
            raise RuntimeError("天猫会话池只能在浏览器运行时中使用")
        if self._tmall_sessions is None:
            from uploader.tmall_session import TmallSessionPool

            self._tmall_sessions = TmallSessionPool(
                idle_timeout_seconds=self.idle_timeout_seconds,
                max_sessions=self.max_sessions,
            )
        return self._tmall_sessions

    def jd_sessions(self):
        if not self.is_current_loop():
            raise RuntimeError("京东会话池只能在浏览器运行时中使用")
        if self._jd_sessions is None:
            from uploader.jd_session import JdSessionPool

            self._jd_sessions = JdSessionPool(
                idle_timeout_seconds=self.idle_timeout_seconds,
                max_sessions=self.max_sessions,
            )
        return self._jd_sessions

    async def _close_account(self, platform: str, account_file: str) -> None:
        if platform == "tmall" and self._tmall_sessions is not None:
            await self._tmall_sessions.close_account(account_file)
        elif platform == "jd" and self._jd_sessions is not None:
            await self._jd_sessions.close_account(account_file)

    def close_account(self, platform: str, account_file: str) -> None:
        if not self.started:
            return
        self.run(self._close_account(platform, account_file))

    async def _close_async(self) -> None:
        if self._tmall_sessions is not None:
            await self._tmall_sessions.close()
            self._tmall_sessions = None
        if self._jd_sessions is not None:
            await self._jd_sessions.close()
            self._jd_sessions = None

    def shutdown(self) -> None:
        with self._guard:
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            self.run(self._close_async())
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=15)
            with self._guard:
                self._loop = None
                self._thread = None
                self._ready.clear()

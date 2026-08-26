# -*- coding: utf-8 -*-
"""
uploader.browser_session 模块

定义浏览器会话（BrowserSession）与会话池（BrowserSessionPool）。

设计目标：
- 每个店铺账号复用同一个 Microsoft Edge 进程与 BrowserContext，避免每次
  任务都冷启动浏览器（冷启动约 5-10 秒，复用后仅需新建 Page）。
- 同一账号严格串行执行浏览器任务（FIFO）；不同账号可通过会话池并行。
- 默认不回收空闲浏览器（idle_timeout_seconds=0）；如需定时回收，可由
  webapp/api/main.py 的 WebSettings.browser_idle_timeout_seconds 配置。
- 每条发布任务使用独立 Page，任务结束后页面保留在 Edge 中，供人工复核。

会话池采用 LRU（最近最少使用）淘汰策略，默认每个平台最多保留 2 个账号
会话；当超出上限时淘汰最久未使用的空闲会话。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from patchright.async_api import Browser, BrowserContext, Playwright, async_playwright

from utils.base_social_media import set_init_script
from utils.config import LOCAL_EDGE_PATH


async def launch_browser(playwright: Playwright, headless: bool) -> Browser:
    """启动 Microsoft Edge 浏览器实例。

    优先使用 LOCAL_EDGE_PATH（MPAU_EDGE_PATH 环境变量）指定的可执行文件路径，
    否则通过 channel="msedge" 让 Playwright 自动查找系统安装的 Edge。

    :param playwright: Playwright 实例
    :param headless: 是否无头模式（登录任务必须为 False 让用户可见浏览器）
    :returns: 已启动的 Browser 对象
    """
    if LOCAL_EDGE_PATH:
        return await playwright.chromium.launch(
            headless=headless,
            executable_path=LOCAL_EDGE_PATH,
        )
    return await playwright.chromium.launch(headless=headless, channel="msedge")


async def launch_chrome_browser(playwright: Playwright, headless: bool) -> Browser:
    """启动 Chrome 浏览器实例，供小红书/抖音等社媒平台会话池使用。"""
    from utils.config import LOCAL_CHROME_PATH

    if LOCAL_CHROME_PATH:
        return await playwright.chromium.launch(
            headless=headless,
            executable_path=LOCAL_CHROME_PATH,
        )
    return await playwright.chromium.launch(headless=headless, channel="chrome")


class BrowserSession:
    """单个店铺账号对应的可复用浏览器会话。

    持有一个 Browser 和一个 BrowserContext，并通过 account_file
    （Playwright storage_state JSON 文件）持久化 Cookie 状态。

    生命周期由 BrowserSessionPool 统一管理，不应直接实例化。
    """

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
        """初始化会话。

        :param playwright: 所属 Playwright 实例（由会话池统一持有）
        :param account_file: 账号 Cookie 状态文件路径
        :param headless: 是否无头模式
        :param logger: loguru 日志器（tmall_logger 或 jd_logger）
        :param platform_label: 平台中文名（"天猫"/"京东"），用于日志
        :param viewport: 视口尺寸，天猫 1280x900，京东 1440x900
        :param launcher: 浏览器启动函数，便于测试注入 mock
        """
        self.playwright = playwright
        self.account_file = Path(account_file).resolve()
        self.headless = headless
        self.logger = logger
        self.platform_label = platform_label
        self.viewport = viewport
        self.launcher = launcher
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        # busy 计数器：同一账号同一时刻可能有多个任务想使用此会话，
        # 但实际上同一账号严格串行，busy 主要用于会话池判断会话是否空闲。
        self.busy = 0
        # 最近使用时间（monotonic），用于 LRU 淘汰与空闲回收
        self.last_used_at = time.monotonic()
        # 鉴权缓存：Cookie 校验结果在 max_age_seconds 内可复用，避免频繁打开页面
        self.last_auth_check_at = 0.0
        self.last_auth_result = False

    @property
    def key(self) -> str:
        """会话在池中的唯一键（即 account_file 的绝对路径字符串）。"""
        return str(self.account_file)

    @property
    def is_connected(self) -> bool:
        """浏览器进程是否仍然连接且上下文存在。"""
        return bool(self.browser and self.browser.is_connected() and self.context)

    def touch(self) -> None:
        """更新最近使用时间，用于 LRU 淘汰判断。"""
        self.last_used_at = time.monotonic()

    def auth_is_fresh(self, max_age_seconds: float) -> bool:
        """Cookie 校验结果是否仍在缓存有效期内。

        :param max_age_seconds: 缓存有效期秒数，<=0 表示不使用缓存
        :returns: True 表示可直接复用上次校验结果，False 需重新校验
        """
        return (
            self.last_auth_result
            and self.last_auth_check_at > 0
            and time.monotonic() - self.last_auth_check_at <= max_age_seconds
        )

    def mark_authenticated(self, authenticated: bool) -> None:
        """记录本次 Cookie 校验结果并刷新使用时间。"""
        self.last_auth_result = authenticated
        self.last_auth_check_at = time.monotonic()
        self.touch()

    async def ensure_open(self) -> BrowserContext:
        """确保浏览器与上下文已打开，返回可用的 BrowserContext。

        若已连接则直接复用；否则先关闭旧会话再启动新浏览器。
        若 account_file 存在则载入 storage_state（Cookie），载入失败时
        降级为空白会话等待重新登录。
        """
        if self.is_connected:
            self.touch()
            assert self.context is not None
            return self.context

        await self.close()
        self.browser = await self.launcher(self.playwright, self.headless)
        context_options: dict[str, object] = {"viewport": self.viewport}
        context_options.update(self.context_options())
        if self.account_file.is_file():
            context_options["storage_state"] = str(self.account_file)
        try:
            self.context = await self.browser.new_context(**context_options)
        except Exception:
            # storage_state 文件损坏或过期时，降级为空白会话等待重新登录
            if "storage_state" not in context_options:
                raise
            self.logger.warning("cookie 状态文件无法载入，将使用空白会话等待重新登录")
            fallback_options = dict(context_options)
            fallback_options.pop("storage_state", None)
            self.context = await self.browser.new_context(**fallback_options)
        # 注入 stealth 反检测脚本（utils/stealth.min.js）
        self.context = await set_init_script(self.context)
        self.touch()
        return self.context

    def context_options(self) -> dict[str, object]:
        """Return platform-specific BrowserContext options."""
        return {}

    async def save_storage_state(self) -> None:
        """将当前 BrowserContext 的 storage_state（Cookie）保存到 account_file。

        先写入同目录临时文件，再原子替换正式文件，避免任务中断时留下半截
        Cookie。文件权限收紧为 0600，仅当前 OS 用户可读。
        """
        if not self.context:
            return
        self.account_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_file = self.account_file.with_name(f".{self.account_file.name}.tmp")
        try:
            await self.context.storage_state(path=str(temporary_file))
            temporary_file.chmod(0o600)
            os.replace(temporary_file, self.account_file)
            self.account_file.chmod(0o600)
        finally:
            with suppress(FileNotFoundError):
                temporary_file.unlink()
        self.touch()

    async def close(self) -> None:
        """关闭 BrowserContext 与 Browser，并重置鉴权缓存。

        关闭异常被抑制（with suppress），避免清理失败影响后续会话创建。
        """
        context, browser = self.context, self.browser
        self.context = None
        self.browser = None
        # 重置鉴权缓存，下次使用时重新校验
        self.last_auth_check_at = 0.0
        self.last_auth_result = False
        if context:
            with suppress(Exception):
                await context.close()
        if browser:
            with suppress(Exception):
                await browser.close()


class BrowserSessionPool:
    """按平台维护的浏览器会话池。

    每个平台（天猫/京东）独立持有一个 BrowserSessionPool，通过 lease()
    方法以上下文管理器形式租借会话。会话池保证：
    - 同一 account_file 在同一时刻只有一个活跃 lease
    - 每个账号会话复用，直到显示模式切换或浏览器断开才重建
    - 达到 max_sessions 上限时按 LRU 淘汰空闲会话
    - idle_timeout_seconds > 0 时启动后台回收任务，关闭空闲超时会话
    """

    session_class = BrowserSession

    def __init__(
        self,
        *,
        logger: Any,
        platform_label: str,
        viewport: dict[str, int],
        idle_timeout_seconds: float = 0,
        max_sessions: int = 2,
        playwright_starter: Callable[[], Awaitable[Playwright]] | None = None,
        launcher: Callable[[Playwright, bool], Awaitable[Browser]] = launch_browser,
    ) -> None:
        """初始化会话池。

        :param logger: 平台日志器
        :param platform_label: 平台中文名
        :param viewport: 视口尺寸
        :param idle_timeout_seconds: 空闲多久后回收浏览器，0 表示不回收
        :param max_sessions: 同一平台最多保留多少账号会话
        :param playwright_starter: 自定义 Playwright 启动器（测试可注入 mock）
        :param launcher: 浏览器启动函数
        """
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
        """启动 Playwright 实例（异步）。"""
        return await async_playwright().start()

    @staticmethod
    def _key(account_file: str | Path) -> str:
        """根据 account_file 生成会话池键（绝对路径字符串）。"""
        return str(Path(account_file).resolve())

    @property
    def session_count(self) -> int:
        """当前池中会话数量。"""
        return len(self._sessions)

    async def _ensure_playwright(self) -> Playwright:
        """确保 Playwright 实例已启动。"""
        if self._closed:
            raise RuntimeError(f"{self.platform_label}浏览器会话池已经关闭")
        if self._playwright is None:
            self._playwright = await self._playwright_starter()
        return self._playwright

    def _ensure_reaper(self) -> None:
        """确保空闲回收后台任务已启动（仅当 idle_timeout_seconds > 0）。"""
        if self._closed:
            raise RuntimeError(f"{self.platform_label}浏览器会话池已经关闭")
        if self.idle_timeout_seconds <= 0 or self._reaper_task is not None:
            return
        self._reaper_task = asyncio.create_task(
            self._reap_loop(),
            name=f"{self.platform_label.lower()}-session-reaper",
        )

    async def _reap_loop(self) -> None:
        """空闲回收后台循环。

        每隔 idle_timeout_seconds/2（最多 60 秒）检查一次，关闭空闲超时会话。
        """
        interval = min(60.0, max(1.0, self.idle_timeout_seconds / 2))
        try:
            while True:
                await asyncio.sleep(interval)
                await self.reap_idle()
        except asyncio.CancelledError:
            return

    async def _discard_locked(self, key: str) -> None:
        """在持锁状态下丢弃指定会话，并保存其 Cookie（若已认证）。"""
        session = self._sessions.pop(key, None)
        if session:
            # 已认证的会话在丢弃前保存 Cookie，避免下次重新登录
            if session.last_auth_result:
                with suppress(Exception):
                    await session.save_storage_state()
            await session.close()

    async def _evict_lru_locked(self) -> None:
        """LRU 淘汰：关闭最久未使用的空闲会话，为新会话腾出位置。"""
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
        """租借指定账号的浏览器会话（上下文管理器）。

        :param account_file: 账号 Cookie 文件路径
        :param headless: 请求的无头模式
        :param preserve_existing_mode: True 时复用已有会话的显示模式
            （用于 Cookie 校验任务，不打断用户可见的浏览器）
        :yields: 可用的 BrowserSession

        异常情况：
        - 同一账号会话仍在使用中且需要切换显示模式 → RuntimeError
        - 池已关闭 → RuntimeError
        - 会话冷启动失败 → 抛出原异常（会话已清理）
        """
        key = self._key(account_file)
        async with self._lock:
            self._ensure_reaper()
            session = self._sessions.get(key)
            # 决定本次使用何种显示模式
            requested_headless = session.headless if session and preserve_existing_mode else headless
            # 显示模式变化或连接断开时需要重建会话
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
                # 达到上限时先淘汰 LRU 空闲会话
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
            # 释放时减少 busy 计数并刷新使用时间
            async with self._lock:
                current = self._sessions.get(key)
                if current is session:
                    # 京东/天猫会在页面访问期间刷新 Cookie；即使任务失败，也要把
                    # 最新状态落盘，避免下一次任务继续使用旧会话。
                    if session.last_auth_result:
                        with suppress(Exception):
                            await session.save_storage_state()
                    session.busy = max(0, session.busy - 1)
                    session.touch()

    async def reap_idle(self) -> None:
        """立即回收空闲超时的会话（由 _reap_loop 周期调用，也可手动触发）。"""
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
        """关闭指定账号的会话（用于删除账号时清理浏览器）。"""
        key = self._key(account_file)
        async with self._lock:
            session = self._sessions.get(key)
            if session and session.busy:
                raise RuntimeError("账号浏览器会话仍在执行任务，暂时不能关闭")
            await self._discard_locked(key)

    async def close(self) -> None:
        """关闭整个会话池：取消回收任务、丢弃所有会话、停止 Playwright。"""
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

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from loguru import logger

from uploader.jd_uploader.session import JdSessionPool
from uploader.tmall_uploader.session import TmallSessionPool
from webapp.api.browser_runtime import BrowserRuntime
from webapp.api.models import validate_publish_request
from webapp.api.platforms import TmallVideoUploadRequest, upload_tmall_video
from webapp.api.store import JobStore
from webapp.api.tasks import TaskManager as _TaskManager
from webapp.workspaces import AppDataPaths


TEST_USER_ID = "0" * 32


def TaskManager(store: JobStore, **kwargs) -> _TaskManager:
    """Build a task manager with production-shaped isolated test paths."""
    paths = AppDataPaths.create(
        store.data_dir.parent / f".{store.data_dir.name}-test-users"
    ).for_user(TEST_USER_ID)
    job_log_dir = kwargs.pop("job_log_dir", None)
    if job_log_dir is not None:
        job_log_dir.mkdir(parents=True, exist_ok=True)
        paths = replace(paths, job_logs=job_log_dir)
    return _TaskManager(store, user_id=TEST_USER_ID, paths=paths, **kwargs)


class FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.storage_paths: list[str] = []
        self.init_scripts: list[str] = []

    async def add_init_script(self, *, path: str) -> None:
        self.init_scripts.append(path)

    async def storage_state(self, *, path: str) -> None:
        self.storage_paths.append(path)
        Path(path).write_text("{}", encoding="utf-8")

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.connected = True
        self.contexts: list[FakeContext] = []
        self.context_options: list[dict] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **options) -> FakeContext:
        self.context_options.append(options)
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.connected = False


class FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class TmallSessionPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.account_file = Path(self.temp_dir.name) / "tmall_shop1.json"
        self.playwright = FakePlaywright()
        self.browsers: list[FakeBrowser] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def make_playwright(self):
        return self.playwright

    async def launch_browser(self, _playwright, _headless):
        browser = FakeBrowser()
        self.browsers.append(browser)
        return browser

    def make_pool(self, *, idle_timeout_seconds: float = 300) -> TmallSessionPool:
        return TmallSessionPool(
            idle_timeout_seconds=idle_timeout_seconds,
            max_sessions=2,
            playwright_starter=self.make_playwright,
            launcher=self.launch_browser,
        )

    def test_same_account_and_mode_reuses_browser_and_context(self):
        async def scenario():
            pool = self.make_pool()
            try:
                async with pool.lease(self.account_file, headless=False) as first:
                    first_browser = first.browser
                    first_context = first.context
                async with pool.lease(self.account_file, headless=False) as second:
                    self.assertIs(second, first)
                    self.assertIs(second.browser, first_browser)
                    self.assertIs(second.context, first_context)
                self.assertEqual(len(self.browsers), 1)
            finally:
                await pool.close()

            self.assertFalse(self.browsers[0].connected)
            self.assertTrue(self.playwright.stopped)

        asyncio.run(scenario())

    def test_mode_change_restarts_only_that_account_session(self):
        async def scenario():
            pool = self.make_pool()
            try:
                async with pool.lease(self.account_file, headless=False) as first:
                    first.mark_authenticated(True)
                    first_browser = first.browser
                async with pool.lease(self.account_file, headless=True) as second:
                    self.assertIsNot(second, first)
                    self.assertTrue(second.headless)
                    self.assertFalse(first_browser.is_connected())
                self.assertEqual(len(self.browsers), 2)
                self.assertTrue(self.account_file.is_file())
            finally:
                await pool.close()

        asyncio.run(scenario())

    def test_idle_reaper_does_not_close_busy_session(self):
        async def scenario():
            pool = self.make_pool(idle_timeout_seconds=0.01)
            try:
                async with pool.lease(self.account_file, headless=True) as session:
                    session.last_used_at = time.monotonic() - 10
                    await pool.reap_idle()
                    self.assertEqual(pool.session_count, 1)
                session.last_used_at = time.monotonic() - 10
                await pool.reap_idle()
                self.assertEqual(pool.session_count, 0)
            finally:
                await pool.close()

        asyncio.run(scenario())


class JdSessionPoolTests(unittest.TestCase):
    def test_same_account_reuses_jd_browser_and_context(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                account_file = Path(temp_dir) / "jd_shop1.json"
                playwright = FakePlaywright()
                browsers = []

                async def start_playwright():
                    return playwright

                async def launch_browser(_playwright, _headless):
                    browser = FakeBrowser()
                    browsers.append(browser)
                    return browser

                pool = JdSessionPool(
                    playwright_starter=start_playwright,
                    launcher=launch_browser,
                )
                try:
                    async with pool.lease(account_file, headless=False) as first:
                        first_browser = first.browser
                        first_context = first.context
                    async with pool.lease(account_file, headless=False) as second:
                        self.assertIs(second, first)
                        self.assertIs(second.browser, first_browser)
                        self.assertIs(second.context, first_context)
                    self.assertEqual(len(browsers), 1)
                finally:
                    await pool.close()

                self.assertFalse(browsers[0].connected)
                self.assertTrue(playwright.stopped)

        asyncio.run(scenario())


class BrowserRuntimeTests(unittest.TestCase):
    def test_coroutines_share_one_long_lived_event_loop(self):
        runtime = BrowserRuntime(user_id=TEST_USER_ID)

        async def loop_identity():
            return id(asyncio.get_running_loop())

        try:
            first = runtime.run(loop_identity())
            second = runtime.run(loop_identity())
            self.assertEqual(first, second)
        finally:
            runtime.shutdown()

        self.assertFalse(runtime.started)


class PooledPlatformAdapterTests(unittest.TestCase):
    def test_tmall_upload_uses_the_leased_session(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        paths = AppDataPaths.create(Path(temp_dir.name) / "data").for_user(
            TEST_USER_ID
        )
        request = TmallVideoUploadRequest(
            account_name="shop1",
            video_file=Path("/tmp/demo.mp4"),
            title="夏季女鞋测评",
            description="轻便好穿",
            tags=["女鞋"],
            headless=False,
        )
        account_file = Path("/tmp/tmall_shop1.json")
        leased_session = object()

        class FakePool:
            @asynccontextmanager
            async def lease(self, path, *, headless, preserve_existing_mode=False):
                self.path = path
                self.headless = headless
                self.preserve_existing_mode = preserve_existing_mode
                yield leased_session

        pool = FakePool()
        setup = AsyncMock(return_value=True)
        with patch("webapp.api.platforms.resolve_account_file", return_value=account_file), patch(
            "webapp.api.platforms.tmall_setup", new=setup
        ), patch("webapp.api.platforms.TmallVideo") as uploader_type:
            uploader_type.return_value.upload_in_session = AsyncMock()
            result = asyncio.run(
                upload_tmall_video(request, paths=paths, session_pool=pool)
            )

        self.assertEqual(result, {})
        self.assertEqual(pool.path, account_file)
        self.assertFalse(pool.headless)
        setup.assert_awaited_once()
        self.assertIs(setup.await_args.kwargs["session"], leased_session)
        uploader_type.return_value.upload_in_session.assert_awaited_once_with(leased_session)
        uploader_type.return_value.main.assert_not_called()


class TaskManagerBrowserRuntimeIntegrationTests(unittest.TestCase):
    def test_sequential_tmall_jobs_receive_the_same_session_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "demo.mp4"
            video.write_bytes(b"video")
            request = validate_publish_request(
                platform="tmall",
                account="shop1",
                video_path=video,
                original_filename=video.name,
                title="夏季女鞋测评",
                headed=False,
            )
            store = JobStore(root / "state")
            manager = TaskManager(store, max_workers=1)
            pools = []

            async def fake_upload(_request, *, paths=None, session_pool=None):
                pools.append(session_pool)

            try:
                with patch("webapp.api.platforms.upload_tmall_video", new=fake_upload):
                    jobs = [manager.submit_publish_task(request) for _ in range(2)]
                    self.assertTrue(manager.wait_for_account_idle("tmall", "shop1", timeout=2))

                self.assertTrue(all(store.get_job(job["id"])["status"] == "succeeded" for job in jobs))
                self.assertEqual(len(pools), 2)
                self.assertIsNotNone(pools[0])
                self.assertIs(pools[0], pools[1])
            finally:
                manager.shutdown()

    def test_sequential_jd_jobs_receive_the_same_session_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "demo.mp4"
            video.write_bytes(b"video")
            request = validate_publish_request(
                platform="jd",
                account="shop1",
                video_path=video,
                original_filename=video.name,
                title="京东视频标题示例",
                headed=False,
            )
            store = JobStore(root / "state")
            manager = TaskManager(store, max_workers=1)
            pools = []

            async def fake_upload(_request, *, paths=None, session_pool=None):
                pools.append(session_pool)

            try:
                with patch("webapp.api.platforms.upload_jd_video", new=fake_upload):
                    jobs = [manager.submit_publish_task(request) for _ in range(2)]
                    self.assertTrue(manager.wait_for_account_idle("jd", "shop1", timeout=2))

                self.assertTrue(all(store.get_job(job["id"])["status"] == "succeeded" for job in jobs))
                self.assertEqual(len(pools), 2)
                self.assertIsNotNone(pools[0])
                self.assertIs(pools[0], pools[1])
            finally:
                manager.shutdown()

    def test_concurrent_accounts_keep_separate_job_logs_on_shared_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = JobStore(root / "state")
            manager = TaskManager(store, max_workers=2, job_log_dir=root / "logs")

            async def log_for_job(job):
                logger.bind(business_name="tmall").info(f"entry-{job['account']}")
                await asyncio.sleep(0.01)
                return {"message": "complete"}

            manager._run_platform_task_async = log_for_job
            try:
                first = manager.submit_account_task(
                    kind="check", platform="tmall", account="shop1", headed=False
                )
                second = manager.submit_account_task(
                    kind="check", platform="tmall", account="shop2", headed=False
                )
                self.assertTrue(manager.wait_for_account_idle("tmall", "shop1", timeout=2))
                self.assertTrue(manager.wait_for_account_idle("tmall", "shop2", timeout=2))

                first_log = manager.job_log_path(first["id"]).read_text(encoding="utf-8")
                second_log = manager.job_log_path(second["id"]).read_text(encoding="utf-8")
                self.assertIn("entry-shop1", first_log)
                self.assertNotIn("entry-shop2", first_log)
                self.assertIn("entry-shop2", second_log)
                self.assertNotIn("entry-shop1", second_log)
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from local_agent.client import AgentApiError
from local_agent.client import AgentApiClient
from local_agent.credentials import AgentConnectionStore
from local_agent import desktop
from local_agent.desktop import _connect_with_status_window
from local_agent import autostart
from local_agent.local_upload import LocalUploadServer
from local_agent.main import LocalAgentApplication, _agent_log, _server_url
from local_agent.runner import AgentJobRunner
from uploader.errors import PublishResultUncertainError
from webapp.ai_copy.contracts import ProductReference
from webapp.api.agent_tasks import AgentTaskManager
from webapp.api.models import validate_publish_request
from webapp.api.store import JobStore
from webapp.workspaces import AppDataPaths, UserWorkspaceRegistry

USER_ID = "a" * 32
AGENT_ID = "b" * 32


class AgentTaskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app_paths = AppDataPaths.create(self.root / "data")
        self.paths = self.app_paths.for_user(USER_ID)
        self.store = JobStore(self.paths.runtime)
        self.manager = AgentTaskManager(
            self.store,
            user_id=USER_ID,
            paths=self.paths,
        )

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.temporary.cleanup()

    def connect(self) -> None:
        self.manager.connect_agent(
            agent_id=AGENT_ID,
            device_name="Operator-PC",
            system="Windows 11",
            version="test",
        )

    def test_default_workspace_never_creates_a_cloud_browser_runtime(self):
        registry = UserWorkspaceRegistry(
            self.app_paths,
            user_workers=1,
            global_browser_tasks=10,
            browser_idle_timeout_seconds=300,
        )
        try:
            workspace = registry.get("c" * 32)
            self.assertTrue(workspace.task_manager.remote_execution)
            self.assertIsNone(workspace.task_manager.browser_runtime)
        finally:
            registry.close()

    def test_agent_claims_and_completes_a_queued_job(self):
        job = self.manager.submit_account_task(
            kind="login", platform="tmall", account="shop1", headed=True
        )
        self.assertEqual(self.store.get_job(job["id"])["status"], "queued")
        self.assertIsNone(self.manager.browser_runtime)

        self.connect()
        claimed = self.manager.claim_next_job(AGENT_ID)
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "running")
        with patch.object(self.store, "_write", wraps=self.store._write) as write:
            heartbeat = self.manager.heartbeat(job["id"], AGENT_ID)
        self.assertFalse(heartbeat["cancel_requested"])
        self.assertEqual(write.call_count, 0)

        completed = self.manager.complete_agent_job(
            job["id"],
            AGENT_ID,
            status="succeeded",
            message="local login complete",
            error="",
            result={"ok": True},
            logs=["local edge log"],
        )
        self.assertEqual(completed["status"], "succeeded")
        self.assertIn(
            "local edge log",
            self.manager.job_log_path(job["id"]).read_text(encoding="utf-8"),
        )

    def _drop_agent_as_offline(self) -> None:
        """Push the agent past the offline threshold and run the reaper."""
        self.manager._agents[AGENT_ID]["last_seen_monotonic"] = (
            time.monotonic() - self.manager._offline_after_seconds() - 1
        )
        self.manager._drop_offline_agents_locked(time.monotonic())
        self.assertNotIn(AGENT_ID, self.manager._agents, "代理应已被判定离线")

    def test_dropped_agent_is_revived_on_heartbeat_instead_of_failing(self):
        # 续约晚了（例如被长任务阻塞）会被判离线。此时带着有效设备令牌发来的
        # 心跳应恢复在线，而不是报错让客户端取消任务。
        job = self.manager.submit_account_task(
            kind="login", platform="tmall", account="shop1", headed=True
        )
        self.connect()
        self.assertEqual(self.manager.claim_next_job(AGENT_ID)["id"], job["id"])

        self._drop_agent_as_offline()
        heartbeat = self.manager.heartbeat(job["id"], AGENT_ID)

        self.assertFalse(heartbeat["cancel_requested"])
        self.assertIn(AGENT_ID, self.manager._agents)
        self.assertEqual(
            self.manager._agents[AGENT_ID]["device_name"],
            "Operator-PC",
            "恢复在线时应保留原设备信息",
        )

    def test_dropped_agent_is_revived_on_claim(self):
        job = self.manager.submit_account_task(
            kind="check", platform="tmall", account="shop1", headed=False
        )
        self.connect()
        self._drop_agent_as_offline()

        claimed = self.manager.claim_next_job(AGENT_ID)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job["id"])
        self.assertIn(AGENT_ID, self.manager._agents)

    def test_agent_claim_wait_is_woken_when_a_job_is_created(self):
        self.connect()

        async def scenario():
            waiter = asyncio.create_task(
                self.manager.wait_for_claimable_job(AGENT_ID, timeout_seconds=1)
            )
            await asyncio.sleep(0.01)
            created = self.manager.submit_account_task(
                kind="check", platform="tmall", account="shop1", headed=False
            )
            claimed = await waiter
            return created, claimed

        created, claimed = asyncio.run(scenario())
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], created["id"])

    def test_publish_lease_expiry_becomes_uncertain_and_cleans_upload(self):
        video_dir = self.paths.uploads / ("d" * 32)
        video_dir.mkdir()
        video = video_dir / "demo.mp4"
        video.write_bytes(b"video")
        request = validate_publish_request(
            platform="tmall",
            cover_ratio="original",
            account="shop1",
            video_path=video,
            original_filename=video.name,
            title="本地代理流程验证",
            managed_upload=True,
        )
        job = self.manager.submit_publish_task(request)
        self.connect()
        self.manager.claim_next_job(AGENT_ID)
        self.store.update_job(
            job["id"],
            lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )

        expired = self.manager.reap_expired_jobs()
        self.assertEqual(expired[0]["status"], "uncertain")
        self.assertFalse(video_dir.exists())

    def test_only_one_device_can_connect_to_the_same_user(self):
        self.connect()
        with self.assertRaisesRegex(RuntimeError, "同时只能连接一台电脑"):
            self.manager.connect_agent(
                agent_id="e" * 32,
                device_name="Another-PC",
                system="Windows 11",
                version="test",
            )

    def test_disconnect_agent_removes_presence_without_affecting_pairing_state(self):
        self.connect()
        self.assertTrue(self.manager.agent_status()["online"])
        self.manager.disconnect_agent(AGENT_ID)
        status = self.manager.agent_status()
        self.assertFalse(status["online"])
        self.assertEqual(status["agents"], [])

    def test_agent_presence_expires_after_the_offline_window(self):
        self.connect()
        offline_after = self.manager.agent_status()["offline_after_seconds"]
        self.manager._agents[AGENT_ID]["last_seen_monotonic"] = (
            time.monotonic() - offline_after - 1
        )
        self.assertFalse(self.manager.agent_status()["online"])

    def test_disconnect_notifies_once_and_ignores_network_failure(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def disconnect(self, _agent_id):
                self.calls += 1
                raise AgentApiError("offline")

        with tempfile.TemporaryDirectory() as temp_dir:
            client = Client()
            application = LocalAgentApplication(
                client, data_root=Path(temp_dir) / "agent", poll_seconds=1
            )
            application.disconnect()
            application.disconnect()
        self.assertEqual(client.calls, 1)

    def test_batch_publish_is_persisted_with_one_state_write(self):
        video = self.paths.media / "demo.mp4"
        video.write_bytes(b"video")
        request = validate_publish_request(
            platform="tmall",
            cover_ratio="original",
            account="shop1",
            video_path=video,
            original_filename=video.name,
            title="批量持久化测试",
        )
        self.manager.start()

        with patch.object(self.store, "_write", wraps=self.store._write) as write:
            jobs = self.manager.submit_publish_tasks(
                [(request, row) for row in range(1, 201)], batch_id="batch-test"
            )

        self.assertEqual(len(jobs), 200)
        self.assertEqual(write.call_count, 1)

    def test_browser_direct_asset_job_persists_ids_without_local_paths_or_ticket(self):
        self.connect()
        ticket = self.manager.issue_local_upload_ticket(
            agent_id=AGENT_ID,
            origin="https://console.example",
            filename="demo.mp4",
            size=5,
            kind="video",
            max_size=100,
        )
        authorized = self.manager.authorize_local_upload(
            ticket=ticket["ticket"],
            agent_id=AGENT_ID,
            origin="https://console.example",
            reserve=True,
        )
        completed = self.manager.complete_local_upload(
            ticket=ticket["ticket"],
            agent_id=AGENT_ID,
            origin="https://console.example",
            sha256="a" * 64,
            size=5,
        )
        fixture = self.paths.runtime / "validation.mp4"
        fixture.write_bytes(b"video")
        request = validate_publish_request(
            platform="tmall",
            cover_ratio="original",
            account="shop1",
            video_path=fixture,
            original_filename="demo.mp4",
            title="本机直传任务",
        )
        public_asset = {
            key: completed[key]
            for key in ("asset_id", "filename", "size", "kind", "sha256")
        }

        job = self.manager.submit_publish_task(
            request,
            local_assets={"video": public_asset, "cover": None, "images": []},
        )

        payload = self.store.get_job(job["id"])["payload"]
        self.assertIsNone(payload["video_path"])
        self.assertEqual(payload["image_paths"], [])
        self.assertIsNone(payload["cover_image_path"])
        self.assertEqual(payload["local_assets"]["video"]["asset_id"], authorized["asset_id"])
        self.assertNotIn(ticket["ticket"], str(payload))

    def test_tmall_product_lookup_round_trips_through_agent_job(self):
        self.connect()
        result: dict[str, ProductReference] = {}
        error: list[Exception] = []

        def lookup() -> None:
            try:
                result["reference"] = self.manager.inspect_tmall_product(
                    "https://detail.tmall.com/item.htm?id=123", timeout_seconds=5
                )
            except Exception as exc:
                error.append(exc)

        worker = threading.Thread(target=lookup)
        worker.start()
        claimed = None
        for _ in range(50):
            claimed = self.manager.claim_next_job(AGENT_ID)
            if claimed is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["kind"], "inspect_product")
        self.manager.complete_agent_job(
            claimed["id"],
            AGENT_ID,
            status="succeeded",
            message="product read",
            error="",
            result={
                "reference": {
                    "source_url": "https://detail.tmall.com/item.htm?id=123",
                    "title": "测试商品",
                    "summary": "商品摘要",
                    "attributes": {},
                }
            },
            logs=[],
        )
        worker.join(timeout=3)

        self.assertEqual(error, [])
        self.assertEqual(result["reference"].title, "测试商品")
        self.assertNotIn(
            "product-lookup", [item["account"] for item in self.store.list_accounts()]
        )
        self.assertIsNone(self.store.get_job(claimed["id"]))


class AgentConnectionStoreTests(unittest.TestCase):
    def test_paired_connection_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AgentConnectionStore(Path(temp_dir) / "agent")
            store.save(
                server_url="https://mpau.example.com/",
                agent_token="secret-device-token",
                user={
                    "id": USER_ID,
                    "username": "operator1",
                    "display_name": "Operator One",
                    "role": "operator",
                },
                expires_at="2099-01-01T00:00:00+00:00",
            )

            loaded = store.load()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.server_url, "https://mpau.example.com")
            self.assertEqual(loaded.agent_token, "secret-device-token")
            self.assertEqual(loaded.user["id"], USER_ID)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

            store.clear()
            self.assertIsNone(store.load())


class AgentAutostartTests(unittest.TestCase):
    def test_development_autostart_uses_desktop_module(self):
        with patch.object(autostart.sys, "frozen", False, create=True):
            arguments = autostart.autostart_arguments()
        self.assertEqual(arguments[-3:], ["-m", "local_agent.desktop", "--background"])

    def test_server_url_accepts_direct_http_and_https(self):
        self.assertEqual(
            _server_url("http://10.31.108.221:8788/"),
            "http://10.31.108.221:8788",
        )
        self.assertEqual(_server_url("https://publish.example.com"), "https://publish.example.com")

    def test_installer_creates_desktop_shortcut_by_default(self):
        installer = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "windows"
            / "mpau-agent-installer.iss"
        ).read_text(encoding="utf-8")
        task_line = next(
            line for line in installer.splitlines() if line.startswith('Name: "desktopicon"')
        )
        self.assertNotIn("unchecked", task_line.casefold())

    def test_installer_closes_old_helper_and_offers_restart(self):
        installer = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "windows"
            / "mpau-agent-installer.iss"
        ).read_text(encoding="utf-8")
        self.assertIn("CloseApplications=yes", installer)
        run_line = next(
            line for line in installer.splitlines() if line.startswith("Filename: \"{app}")
        )
        self.assertIn("postinstall", run_line)
        self.assertNotIn("runhidden", run_line)

    def test_double_click_start_keeps_tray_and_opens_status_window(self):
        connection = SimpleNamespace(
            server_url="http://10.31.108.221:8788",
            agent_token="token",
        )
        store = SimpleNamespace(load=lambda: connection, clear=lambda: None)
        application = SimpleNamespace(
            authorization_failed=False,
            stop=lambda: None,
            disconnect=lambda: None,
            run=lambda **_kwargs: None,
        )

        class FakeThread:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            desktop.os, "name", "nt"
        ), patch.object(
            desktop,
            "build_parser",
            return_value=SimpleNamespace(
                parse_args=lambda: SimpleNamespace(
                    background=False,
                    data_dir=Path(temp_dir),
                )
            ),
        ), patch.object(
            desktop, "_acquire_single_instance", return_value=True
        ), patch.object(
            desktop, "AgentConnectionStore", return_value=store
        ), patch.object(
            desktop, "AgentApiClient", return_value=object()
        ), patch.object(
            desktop, "LocalAgentApplication", return_value=application
        ), patch.object(
            desktop, "_connect_with_status_window", return_value="connected"
        ), patch.object(
            desktop, "_run_tray", return_value=None
        ) as run_tray, patch.object(
            desktop.threading, "Thread", FakeThread
        ), patch.object(
            desktop.theme, "enable_dpi_awareness"
        ):
            desktop.run()

        self.assertTrue(run_tray.call_args.kwargs["show_status_on_start"])

    def test_tray_setup_explicitly_makes_icon_visible(self):
        application = SimpleNamespace(
            client=object(),
            stopping=False,
            authorization_failed=False,
        )
        connection = SimpleNamespace(
            server_url="http://10.31.108.221:8788",
            user={"display_name": "测试账号"},
        )

        class FakeIcon:
            last_instance = None

            def __init__(self, *_args, **_kwargs):
                self.visible = False
                self.stopped = False
                FakeIcon.last_instance = self

            def run(self, setup=None):
                setup(self)
                application.stopping = True

            def stop(self):
                self.stopped = True

        FakeIcon.__module__ = "pystray._win32"

        fake_pystray = SimpleNamespace(
            Icon=FakeIcon,
            Menu=lambda *_items: object(),
            MenuItem=lambda *_args, **_kwargs: object(),
        )
        fake_pystray.Menu.SEPARATOR = object()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, {"pystray": fake_pystray}
        ), patch.object(
            desktop, "_tray_image", return_value=object()
        ), patch.object(
            desktop, "_start_wake_listener", return_value=lambda: None
        ), patch.object(
            desktop, "_start_background_update_checks"
        ):
            outcome = desktop._run_tray(
                application,
                connection,
                SimpleNamespace(),
                Path(temp_dir),
            )

        self.assertEqual(outcome, "quit")
        self.assertTrue(FakeIcon.last_instance.visible)

    def test_missing_tray_backend_stops_with_error_instead_of_fallback(self):
        # 托盘后端都加载不了时同样只影响界面：仍然报错提示，但绝不停止代理，
        # 否则已领取的任务会被服务端按租约超时回收。
        stopped = []
        application = SimpleNamespace(
            stop=lambda: stopped.append(True),
            disconnect=lambda: None,
        )
        connection = SimpleNamespace()
        store = SimpleNamespace()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, {"pystray": None}
        ), patch.object(
            desktop, "_show_fatal_error"
        ) as fatal:
            outcome = desktop._run_tray(
                application,
                connection,
                store,
                Path(temp_dir),
            )

        self.assertEqual(outcome, "tray-failed")
        self.assertEqual(stopped, [], "托盘后端缺失时不应停止代理")
        fatal.assert_called_once()

    def test_invisible_tray_backend_keeps_agent_running(self):
        # 托盘图标起不来只影响界面。代理线程在连接成功后已独立启动，
        # 绝不能因此停止代理，否则正在执行的任务会被服务端按租约回收。
        stopped = []
        application = SimpleNamespace(
            client=object(),
            stopping=False,
            authorization_failed=False,
            stop=lambda: (stopped.append(True), setattr(application, "stopping", True)),
            disconnect=lambda: None,
        )
        connection = SimpleNamespace(
            server_url="http://10.31.108.221:8788",
            user={"display_name": "测试账号"},
        )

        class InvisibleIcon:
            last_instance = None

            def __init__(self, *_args, **_kwargs):
                self._visible = False
                self.set_attempts = 0
                InvisibleIcon.last_instance = self

            @property
            def visible(self):
                return False

            @visible.setter
            def visible(self, _value):
                self._visible = False
                self.set_attempts += 1

            def run(self, setup=None):
                setup(self)

            def stop(self):
                pass

        InvisibleIcon.__module__ = "pystray._win32"
        fake_pystray = SimpleNamespace(
            Icon=InvisibleIcon,
            Menu=lambda *_items: object(),
            MenuItem=lambda *_args, **_kwargs: object(),
        )
        fake_pystray.Menu.SEPARATOR = object()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, {"pystray": fake_pystray}
        ), patch.object(
            desktop, "_tray_image", return_value=object()
        ), patch.object(
            desktop, "_show_fatal_error"
        ) as fatal, patch.object(
            desktop, "_start_wake_listener", return_value=lambda: None
        ), patch.object(
            desktop, "_start_background_update_checks"
        ), patch.object(
            desktop, "TRAY_ICON_RETRY_DELAY_SECONDS", 0.0
        ):
            outcome = desktop._run_tray(
                application,
                connection,
                SimpleNamespace(),
                Path(temp_dir),
            )

        self.assertEqual(outcome, "tray-failed")
        self.assertEqual(stopped, [], "托盘不可见时不应停止代理")
        self.assertEqual(
            InvisibleIcon.last_instance.set_attempts,
            desktop.TRAY_ICON_ATTEMPTS,
            "托盘图标显示应重试而不是一次失败就放弃",
        )
        fatal.assert_called_once()


class DesktopConnectionWindowTests(unittest.TestCase):
    class FakeWidget:
        def __init__(self, *_args, **_kwargs):
            self.options = {}

        def pack(self, **_kwargs):
            return self

        def configure(self, **kwargs):
            self.options.update(kwargs)

    class FakeStringVar:
        def __init__(self, value=""):
            self.value = value

        def set(self, value):
            self.value = value

    class FakeRoot(FakeWidget):
        def __init__(self):
            super().__init__()
            self.callbacks = []
            self.destroyed = False
            self.withdrawn = False

        def title(self, _value):
            pass

        def resizable(self, *_args):
            pass

        def protocol(self, *_args):
            pass

        def after(self, _delay, callback):
            self.callbacks.append(callback)
            return callback

        def after_cancel(self, callback):
            if callback in self.callbacks:
                self.callbacks.remove(callback)

        def mainloop(self):
            deadline = time.monotonic() + 2
            while not self.destroyed and time.monotonic() < deadline:
                if self.callbacks:
                    self.callbacks.pop(0)()
                else:
                    time.sleep(0.001)
            if not self.destroyed:
                raise AssertionError("连接窗口测试超时")

        def destroy(self):
            self.destroyed = True

        def withdraw(self):
            self.withdrawn = True

        def deiconify(self):
            self.withdrawn = False

        def lift(self):
            pass

        def focus_force(self):
            pass

        def attributes(self, *_args):
            pass

    def run_connection(self, connect, *, start_hidden=False, reconnect=False):
        root = self.FakeRoot()
        tkinter = SimpleNamespace(
            Tk=lambda: root,
            Frame=self.FakeWidget,
            Label=self.FakeWidget,
            StringVar=self.FakeStringVar,
            messagebox=SimpleNamespace(askyesno=lambda *_args, **_kwargs: reconnect),
        )
        application = SimpleNamespace(
            connect=connect,
            client=SimpleNamespace(server_url="http://10.31.108.221:8788"),
            stopping=False,
        )
        with patch.dict(sys.modules, {"tkinter": tkinter}), patch(
            "local_agent.desktop.theme.header_band", return_value=self.FakeWidget()
        ), patch(
            "local_agent.desktop.theme.primary_button", side_effect=lambda *_args, **_kwargs: self.FakeWidget()
        ), patch(
            "local_agent.desktop.theme.secondary_button", side_effect=lambda *_args, **_kwargs: self.FakeWidget()
        ), patch(
            "local_agent.desktop.theme.apply_tk_scaling"
        ), patch(
            "local_agent.desktop.theme.center_window"
        ), patch(
            "local_agent.desktop._start_wake_listener", return_value=lambda: None
        ):
            outcome = _connect_with_status_window(
                application, start_hidden=start_hidden
            )
        return outcome, root

    def test_successful_connection_advances_to_status_window(self):
        outcome, _root = self.run_connection(lambda: None)
        self.assertEqual(outcome, "connected")

    def test_transient_failure_retries_without_silently_exiting(self):
        attempts = 0

        def connect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise AgentApiError("网络暂不可用")

        outcome, _root = self.run_connection(connect)
        self.assertEqual(outcome, "connected")
        self.assertEqual(attempts, 2)

    def test_background_connection_window_starts_hidden(self):
        outcome, root = self.run_connection(lambda: None, start_hidden=True)
        self.assertEqual(outcome, "connected")
        self.assertTrue(root.withdrawn)

    def test_expired_authorization_returns_to_pairing(self):
        outcome, _root = self.run_connection(
            lambda: (_ for _ in ()).throw(AgentApiError("授权失效", 401))
        )
        self.assertEqual(outcome, "unauthorized")

    def test_three_connection_failures_can_clear_pairing(self):
        attempts = 0

        def connect():
            nonlocal attempts
            attempts += 1
            raise AgentApiError("发布台地址不可达")

        outcome, root = self.run_connection(connect, reconnect=True)

        self.assertEqual(outcome, "re-pair")
        self.assertEqual(attempts, 3)
        self.assertFalse(root.withdrawn)

    def test_declining_repair_keeps_retrying_current_server(self):
        attempts = 0

        def connect():
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise AgentApiError("发布台暂时不可达")

        outcome, _root = self.run_connection(connect, reconnect=False)

        self.assertEqual(outcome, "connected")
        self.assertEqual(attempts, 4)


class LocalAgentApplicationTests(unittest.TestCase):
    def test_agent_log_uses_file_logger_when_windowed_stdio_is_missing(self):
        with patch.object(sys, "stdout", None), patch.object(
            sys, "stderr", None
        ), patch("local_agent.main.logger") as log:
            _agent_log("普通进度")
            _agent_log("错误进度", error=True)

        log.info.assert_called_once_with("普通进度")
        log.warning.assert_called_once_with("错误进度")

    def test_publish_downloads_video_and_cover_before_running(self):
        class Client:
            def __init__(self):
                self.completed = None

            def download_video(
                self, _job_id, _agent_id, destination, *, progress
            ):
                destination.write_bytes(b"video")
                progress()

            def download_cover_image(
                self, _job_id, _agent_id, destination, *, progress
            ):
                destination.write_bytes(b"cover")
                progress()

            def heartbeat(self, _job_id, _agent_id):
                return {"cancel_requested": False}

            def complete(self, _job_id, **payload):
                self.completed = payload

        class CompletedFuture:
            @staticmethod
            def done():
                return True

            @staticmethod
            def result(timeout=None):
                return {"message": "complete"}

        class Runner:
            user_id = USER_ID

            def __init__(self, root):
                self.paths = AppDataPaths.create(root).for_user(USER_ID)
                self.received = None

            def submit(self, _job, video_path, cover_image_path=None):
                self.received = (
                    video_path.read_bytes(),
                    cover_image_path.read_bytes(),
                )
                return CompletedFuture()

            @staticmethod
            def finish_logs(_job_id):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            client = Client()
            application = LocalAgentApplication(
                client, data_root=Path(temp_dir) / "agent", poll_seconds=1
            )
            runner = Runner(Path(temp_dir) / "runner")
            application.runner = runner
            application.execute(
                {
                    "id": "d" * 32,
                    "kind": "publish",
                    "platform": "tmall",
                    "account": "shop1",
                    "payload": {
                        "original_filename": "demo.mp4",
                        "cover_image_filename": "cover-test.png",
                    },
                }
            )

        self.assertEqual(runner.received, (b"video", b"cover"))
        self.assertEqual(client.completed["status"], "succeeded")

    def test_unauthorized_device_stops_and_requests_pairing(self):
        class UnauthorizedClient:
            def claim(self, _agent_id, *, wait_seconds=0):
                raise AgentApiError("设备授权已失效", 401)

        with tempfile.TemporaryDirectory() as temp_dir:
            application = LocalAgentApplication(
                UnauthorizedClient(),
                data_root=Path(temp_dir) / "agent",
                poll_seconds=1,
            )
            application.run(already_connected=True)

        self.assertTrue(application.stopping)
        self.assertTrue(application.authorization_failed)

    def test_transient_heartbeat_failure_does_not_cancel_browser_task(self):
        class Client:
            def __init__(self):
                self.heartbeats = 0
                self.completed = None

            def heartbeat(self, _job_id, _agent_id):
                self.heartbeats += 1
                if self.heartbeats == 1:
                    raise AgentApiError("temporary outage")
                return {"cancel_requested": False}

            def complete(self, _job_id, **payload):
                self.completed = payload

        class PendingFuture:
            def __init__(self):
                self.calls = 0

            def done(self):
                return self.calls >= 2

            def result(self, timeout=None):
                self.calls += 1
                if self.calls == 1 and timeout is not None:
                    raise FutureTimeoutError
                return {"message": "complete"}

        class Runner:
            user_id = USER_ID

            def __init__(self, root):
                self.paths = AppDataPaths.create(root).for_user(USER_ID)
                self.cancelled = False

            def submit(self, _job, _video_path, _cover_image_path=None):
                return PendingFuture()

            def cancel(self, _job_id):
                self.cancelled = True

            def finish_logs(self, _job_id):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            client = Client()
            application = LocalAgentApplication(
                client, data_root=Path(temp_dir) / "agent", poll_seconds=1
            )
            runner = Runner(Path(temp_dir) / "runner")
            application.runner = runner
            application.execute(
                {
                    "id": "f" * 32,
                    "kind": "check",
                    "platform": "tmall",
                    "account": "shop1",
                    "payload": {},
                }
            )

        self.assertFalse(runner.cancelled)
        self.assertEqual(client.completed["status"], "succeeded")

    def test_windowed_logging_uses_file_when_stdout_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = os.environ.copy()
            environment["MPAU_AGENT_DATA_DIR"] = temp_dir
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout = None; sys.stderr = None; import utils.log",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=False,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue((Path(temp_dir) / "logs" / "agent.log").exists())


class AgentJobRunnerTests(unittest.TestCase):
    def test_cancellation_preserves_uncertain_publish_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppDataPaths.create(Path(temp_dir) / "agent-data").for_user(USER_ID)
            runner = AgentJobRunner(USER_ID, paths)
            started = threading.Event()
            job = {
                "id": "e" * 32,
                "kind": "publish",
                "platform": "tmall",
                "account": "shop1",
                "payload": {},
            }

            async def publishing(_job, _video_path, _cover_image_path):
                started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError as exc:
                    raise PublishResultUncertainError(
                        "发布按钮已点击，取消后结果无法确认"
                    ) from exc

            future: Future
            try:
                with patch.object(runner, "_run_job", new=publishing):
                    future = runner.submit(job, None)
                    self.assertTrue(started.wait(timeout=2))
                    runner.cancel(job["id"])
                    with self.assertRaises(PublishResultUncertainError):
                        future.result(timeout=2)
            finally:
                runner.finish_logs(job["id"])
                runner.shutdown()

    def test_tmall_publish_maps_claim_payload_to_local_uploader(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppDataPaths.create(Path(temp_dir) / "agent-data").for_user(USER_ID)
            video = paths.uploads / "demo.mp4"
            video.write_bytes(b"video")
            cover = paths.uploads / "cover.png"
            cover.write_bytes(b"cover")
            runner = AgentJobRunner(USER_ID, paths)
            session_pool = object()
            runner.runtime.tmall_sessions = lambda: session_pool
            job = {
                "id": "f" * 32,
                "kind": "publish",
                "platform": "tmall",
                "account": "shop1",
                "payload": {
                    "headed": True,
                    "schedule": None,
                    "title": "本地代理发布测试",
                    "description": "正文",
                    "tags": ["测试"],
                    "cover_ratio": "1:1",
                    "goods_id": "123",
                    "activity_topic": "",
                    "music_name": "",
                    "creator_declaration": "内容无需标注",
                    "dry_run": True,
                    "original": False,
                },
            }
            try:
                with patch(
                    "local_agent.runner.upload_tmall_video",
                    new=AsyncMock(return_value={"mode": "dry_run"}),
                ) as upload:
                    result = asyncio.run(runner._run_job(job, video, cover))
                request = upload.await_args.args[0]
                self.assertEqual(request.account_name, "shop1")
                self.assertEqual(request.video_file, video)
                self.assertEqual(request.cover_image_file, cover)
                self.assertEqual(request.cover_ratio, "1:1")
                self.assertTrue(request.dry_run)
                self.assertFalse(request.headless)
                self.assertEqual(upload.await_args.kwargs["session_pool"], session_pool)
                self.assertIn("用户电脑", result["message"])
            finally:
                runner.shutdown()

    def test_social_video_publish_uses_resolved_cover_path(self):
        for platform, upload_name, session_name in (
            ("xiaohongshu", "upload_xiaohongshu_video", "xiaohongshu_sessions"),
            ("douyin", "upload_douyin_video", "douyin_sessions"),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp_dir:
                paths = AppDataPaths.create(Path(temp_dir) / "agent-data").for_user(USER_ID)
                video = paths.uploads / "demo.mp4"
                video.write_bytes(b"video")
                cover = paths.uploads / "cover.png"
                cover.write_bytes(b"cover")
                runner = AgentJobRunner(USER_ID, paths)
                session_pool = object()
                setattr(runner.runtime, session_name, lambda pool=session_pool: pool)
                job = {
                    "id": "f" * 32,
                    "kind": "publish",
                    "platform": platform,
                    "account": "shop1",
                    "payload": {
                        "headed": True,
                        "schedule": None,
                        "title": "本地代理发布测试",
                        "description": "正文",
                        "tags": ["测试"],
                        "dry_run": True,
                    },
                }
                try:
                    with patch(
                        f"local_agent.runner.{upload_name}",
                        new=AsyncMock(return_value={"mode": "dry_run"}),
                    ) as upload:
                        asyncio.run(runner._run_job(job, video, cover))
                    request = upload.await_args.args[0]
                    self.assertEqual(request.cover_image_file, cover)
                    self.assertEqual(upload.await_args.kwargs["session_pool"], session_pool)
                finally:
                    runner.shutdown()


class UpdaterTests(unittest.TestCase):
    def test_version_parsing_and_comparison(self) -> None:
        from local_agent.updater import is_newer, parse_version

        self.assertEqual(parse_version("0.3.1"), (0, 3, 1))
        self.assertEqual(parse_version("v1.2"), (1, 2))
        self.assertIsNone(parse_version("abc"))
        self.assertIsNone(parse_version(""))
        self.assertTrue(is_newer("0.3.0", "0.2.9"))
        self.assertTrue(is_newer("0.10.0", "0.9.0"))
        self.assertTrue(is_newer("1.0", "0.9.9"))
        self.assertFalse(is_newer("0.3.0", "0.3.0"))
        self.assertFalse(is_newer("0.3", "0.3.0"))
        self.assertFalse(is_newer("0.2.0", "0.3.0"))

    def test_normalize_release_rejects_invalid_manifests(self) -> None:
        from local_agent.updater import normalize_release

        valid = {
            "version": "0.3.0",
            "sha256": "a" * 64,
            "size": 1024,
            "notes": "修复",
        }
        release = normalize_release(valid)
        self.assertIsNotNone(release)
        self.assertEqual(release["sha256"], "a" * 64)
        self.assertIsNone(normalize_release(None))
        self.assertIsNone(normalize_release({}))
        self.assertIsNone(normalize_release({**valid, "version": "latest"}))
        self.assertIsNone(normalize_release({**valid, "sha256": "xyz"}))

    def test_update_installer_is_started_visible(self) -> None:
        from local_agent import updater

        class FakeProcess:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "directory with spaces"
            with patch.object(
                updater.sys, "frozen", True, create=True
            ), patch.object(
                updater.subprocess, "Popen", return_value=FakeProcess()
            ) as popen, patch.object(updater.time, "sleep"):
                installer = (
                    data_root / "update" / "MPAU-Agent-Setup-0.4.0.exe"
                )
                installer.parent.mkdir(parents=True)
                installer.write_bytes(b"installer")
                updater.launch_update(data_root, installer)

        command = popen.call_args.args[0]
        self.assertEqual(command[0], str(installer.resolve()))
        self.assertIn("/CLOSEAPPLICATIONS", command)
        log_arguments = [argument for argument in command if argument.startswith("/LOG=")]
        self.assertEqual(
            log_arguments,
            [f"/LOG={installer.parent.resolve() / updater.INSTALLER_LOG_NAME}"],
        )
        self.assertNotIn('"', log_arguments[0])
        self.assertNotIn("powershell.exe", command)
        self.assertNotIn("/VERYSILENT", command)
        self.assertNotIn("/SUPPRESSMSGBOXES", command)

    def test_download_installer_reports_total_size_to_progress_callback(self) -> None:
        class FakeResponse:
            headers = {"Content-Length": "6"}

            def __init__(self) -> None:
                self._chunks = [b"abc", b"def", b""]

            def read(self, _size):
                return self._chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeOpener:
            def __init__(self) -> None:
                self.request = None

            def open(self, request, timeout=None):
                self.request = request
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir:
            client = AgentApiClient("https://mpau.example.com", "token")
            client.opener = FakeOpener()
            destination = Path(temp_dir) / "agent.exe"
            progress_calls: list[tuple[int, int | None]] = []

            client.download_installer(
                destination,
                expected_sha256="bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721",
                progress=lambda downloaded, total: progress_calls.append((downloaded, total)),
            )

            self.assertEqual(progress_calls[-1], (6, 6))
            self.assertEqual(destination.read_bytes(), b"abcdef")

    def test_disconnect_posts_authenticated_agent_identity(self) -> None:
        class FakeResponse:
            def read(self):
                return b'{"disconnected_agent_id":"' + b"b" * 32 + b'"}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeOpener:
            def __init__(self):
                self.request = None
                self.timeout = None

            def open(self, request, timeout=None):
                self.request = request
                self.timeout = timeout
                return FakeResponse()

        client = AgentApiClient("https://mpau.example.com", "token")
        opener = FakeOpener()
        client.opener = opener
        client.disconnect("b" * 32)
        self.assertEqual(opener.request.method, "POST")
        self.assertEqual(opener.request.full_url, "https://mpau.example.com/api/agent/disconnect")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer token")
        self.assertEqual(json.loads(opener.request.data), {"agent_id": "b" * 32})
        self.assertEqual(opener.timeout, 3)

    def test_update_keeps_helper_open_when_installer_exits_immediately(self) -> None:
        from local_agent import updater

        class FailedProcess:
            def poll(self):
                return 5

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            updater.sys, "frozen", True, create=True
        ), patch.object(
            updater.subprocess, "Popen", return_value=FailedProcess()
        ), patch.object(updater.time, "sleep"):
            installer = Path(temp_dir) / "update" / "MPAU-Agent-Setup-0.4.0.exe"
            installer.parent.mkdir()
            installer.write_bytes(b"installer")
            with self.assertRaisesRegex(RuntimeError, "错误代码 5"):
                updater.launch_update(Path(temp_dir), installer)

    def test_cleanup_stale_installers_keeps_current(self) -> None:
        from local_agent import updater

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "update"
            directory.mkdir()
            old = directory / "MPAU-Agent-Setup-0.2.0.exe"
            keep = directory / "MPAU-Agent-Setup-0.3.0.exe"
            extra = directory / "MPAU-Agent-Setup.exe"
            for path in (old, keep, extra):
                path.write_bytes(b"x")
            updater.cleanup_stale_installers(Path(temp_dir), keep=keep)
            self.assertFalse(old.exists())
            self.assertFalse(extra.exists())
            self.assertTrue(keep.exists())


class InstallerManifestTests(unittest.TestCase):
    def test_manifest_requires_matching_installer(self) -> None:
        from webapp.api.agent import load_installer_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            installer = Path(temp_dir) / "MPAU-Agent-Setup.exe"
            installer.write_bytes(b"setup-bytes")
            manifest_path = Path(temp_dir) / "agent-installer.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": "0.3.0",
                        "sha256": "b" * 64,
                        "size": len(b"setup-bytes"),
                        "released_at": "2026-08-24T02:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            manifest = load_installer_manifest(installer)
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest["version"], "0.3.0")

            # A size mismatch invalidates the manifest.
            manifest_path.write_text(
                json.dumps({"version": "0.3.0", "sha256": "b" * 64, "size": 1}),
                encoding="utf-8",
            )
            manifest_path.touch()
            self.assertIsNone(load_installer_manifest(installer))

            # Missing manifest or installer yields None as well.
            manifest_path.unlink()
            self.assertIsNone(load_installer_manifest(installer))
            self.assertIsNone(load_installer_manifest(Path(temp_dir) / "missing.exe"))


PREFLIGHT_HEADERS = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
    "Access-Control-Request-Private-Network": "true",
}

UPLOAD_ORIGIN = "https://console.example"


class _FakeUploadClient:
    """Stand in for the cloud broker without any network access."""

    def __init__(self, *, size: int = 5, filename: str = "demo.mp4") -> None:
        self.size = size
        self.filename = filename
        self.authorize_error: Exception | None = None
        self.authorize_calls: list[tuple[str, str, bool]] = []
        self.complete_calls: list[dict] = []
        self._assets = 0

    def authorize_local_upload(self, ticket: str, origin: str, *, reserve: bool = True):
        self.authorize_calls.append((ticket, origin, reserve))
        if self.authorize_error is not None:
            raise self.authorize_error
        self._assets += 1
        return {
            "asset_id": f"{self._assets:032x}",
            "filename": self.filename,
            "size": self.size,
        }

    def complete_local_upload(
        self, ticket: str, origin: str, *, asset_id: str, sha256: str, size: int
    ):
        self.complete_calls.append(
            {
                "ticket": ticket,
                "origin": origin,
                "asset_id": asset_id,
                "sha256": sha256,
                "size": size,
            }
        )
        return {
            "asset_id": asset_id,
            "filename": self.filename,
            "size": size,
            "sha256": sha256,
            "kind": "video",
        }


class LocalUploadCorsTests(unittest.TestCase):
    """Verify the browser contract of the local upload endpoint.

    The web UI posts from a public HTTPS origin to 127.0.0.1, so Chrome always
    sends a private-network preflight first. Any answer without CORS headers -
    errors included - makes the browser raise a TypeError and the UI reports an
    unreachable agent while this server is in fact listening.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset_root = self.root / "assets"
        self.client = _FakeUploadClient()
        self.port = self._free_port()
        self.server = LocalUploadServer(
            self.client, self.asset_root, port=self.port
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _header(headers, name: str) -> str | None:
        for key, value in headers:
            if key.lower() == name.lower():
                return value
        return None

    def _send(
        self,
        method: str,
        path: str,
        *,
        origin: str | None = UPLOAD_ORIGIN,
        host: str | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(headers or {})
        if origin is not None:
            request_headers["Origin"] = origin
        if host is not None:
            request_headers["Host"] = host
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, response.getheaders(), payload
        finally:
            connection.close()

    def _send_incomplete(
        self, origin: str, *, declared_length: int = 4096, sent: bytes = b"short"
    ) -> None:
        """Open a POST, promise more bytes than are delivered, then hang up.

        Windows does not reliably deliver a response after shutdown(SHUT_WR),
        and reading one blocks the test, so the client hangs up without
        reading. Coping with the dead client is the server's job.
        """
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/v1/upload?ticket=ticket-abort")
            connection.putheader("Host", "127.0.0.1")
            connection.putheader("Origin", origin)
            connection.putheader("Content-Type", "text/plain;charset=UTF-8")
            connection.putheader("Content-Length", str(declared_length))
            connection.endheaders()
            connection.send(sent)
        finally:
            connection.close()

    def _wait_for_abort_cleanup(self, timeout: float = 5.0) -> None:
        """Give the server time to notice the hang-up and clean up."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.client.authorize_calls:
                return
            time.sleep(0.05)

    def assertCorsVisible(self, headers, origin: str = UPLOAD_ORIGIN) -> None:
        """A response the browser is allowed to read must echo the origin."""
        self.assertEqual(
            self._header(headers, "Access-Control-Allow-Origin"), origin
        )
        self.assertEqual(self._header(headers, "Vary"), "Origin")
        self.assertEqual(
            self._header(headers, "Access-Control-Allow-Private-Network"), "true"
        )
        self.assertEqual(self._header(headers, "Cache-Control"), "no-store")

    def test_preflight_answers_204_without_contacting_the_cloud(self):
        self.client.authorize_error = AgentApiError("发布台暂时不可用", 502)

        status, headers, body = self._send(
            "OPTIONS", "/v1/upload?ticket=ticket-1", headers=PREFLIGHT_HEADERS
        )

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        # The preflight must not depend on a reachable cloud: the ticket is
        # fully validated by the POST that actually carries the file.
        self.assertEqual(self.client.authorize_calls, [])
        self.assertCorsVisible(headers)
        self.assertEqual(
            self._header(headers, "Access-Control-Allow-Methods"), "POST, OPTIONS"
        )
        self.assertEqual(
            self._header(headers, "Access-Control-Allow-Headers"), "Content-Type"
        )
        self.assertEqual(self._header(headers, "Access-Control-Max-Age"), "300")

    def test_preflight_survives_a_cloud_outage_even_without_a_status_code(self):
        self.client.authorize_error = AgentApiError("连接被重置")

        status, headers, _body = self._send(
            "OPTIONS", "/v1/upload?ticket=ticket-1", headers=PREFLIGHT_HEADERS
        )

        self.assertEqual(status, 204)
        self.assertCorsVisible(headers)

    def test_preflight_without_a_ticket_is_rejected_but_stays_readable(self):
        status, headers, body = self._send(
            "OPTIONS", "/v1/upload", headers=PREFLIGHT_HEADERS
        )

        self.assertEqual(status, 403)
        self.assertCorsVisible(headers)
        self.assertIn("预检失败", json.loads(body)["detail"])

    def test_preflight_rejects_other_paths_with_cors_headers(self):
        status, headers, _body = self._send(
            "OPTIONS", "/v1/anything?ticket=ticket-1", headers=PREFLIGHT_HEADERS
        )

        self.assertEqual(status, 403)
        self.assertCorsVisible(headers)

    def test_preflight_rejects_requests_addressed_to_a_public_host(self):
        status, headers, _body = self._send(
            "OPTIONS",
            "/v1/upload?ticket=ticket-1",
            host="console.example",
            headers=PREFLIGHT_HEADERS,
        )

        self.assertEqual(status, 403)
        self.assertCorsVisible(headers)

    def test_preflight_never_reflects_a_non_web_origin(self):
        for origin in (
            None,
            "",
            "null",
            "file://",
            "evil.example",
            "ftp://console.example",
            "javascript:alert(1)",
            "https://",
        ):
            with self.subTest(origin=origin):
                status, headers, _body = self._send(
                    "OPTIONS",
                    "/v1/upload?ticket=ticket-1",
                    origin=origin,
                    headers=PREFLIGHT_HEADERS,
                )

                self.assertEqual(status, 403)
                self.assertIsNone(
                    self._header(headers, "Access-Control-Allow-Origin")
                )
                self.assertIsNone(
                    self._header(headers, "Access-Control-Allow-Private-Network")
                )

    def test_preflight_accepts_loopback_and_lan_origins(self):
        for origin in (
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://10.31.108.221:8788",
            "https://publish.example.com",
        ):
            with self.subTest(origin=origin):
                status, headers, _body = self._send(
                    "OPTIONS",
                    "/v1/upload?ticket=ticket-1",
                    origin=origin,
                    headers=PREFLIGHT_HEADERS,
                )

                self.assertEqual(status, 204)
                self.assertCorsVisible(headers, origin)

    def test_post_without_a_ticket_answers_403_with_cors_headers(self):
        status, headers, body = self._send("POST", "/v1/upload", body=b"video")

        self.assertEqual(status, 403)
        self.assertCorsVisible(headers)
        self.assertIn("请求无效", json.loads(body)["detail"])

    def test_post_reports_a_cloud_rejection_with_cors_headers(self):
        self.client.authorize_error = AgentApiError("票据已过期", 502)

        status, headers, body = self._send(
            "POST", "/v1/upload?ticket=ticket-1", body=b"video"
        )

        self.assertEqual(status, 502)
        self.assertCorsVisible(headers)
        self.assertEqual(json.loads(body)["detail"], "票据已过期")

    def test_post_size_mismatch_answers_422_with_cors_headers(self):
        status, headers, body = self._send(
            "POST", "/v1/upload?ticket=ticket-1", body=b"vid"
        )

        self.assertEqual(status, 422)
        self.assertCorsVisible(headers)
        self.assertIn("大小与票据不一致", json.loads(body)["detail"])

    def test_post_aborted_transfer_keeps_the_server_alive(self):
        """A browser hanging up mid-upload must not break later uploads.

        The aborted transfer itself cannot be answered: once the client has
        stopped sending, writing a response raises ConnectionAbortedError on
        Windows. What matters is that the worker thread survives the hang-up.
        """
        # The declared length must match the ticket, otherwise the size check
        # short-circuits before the body is read and the abort never happens.
        self.client.size = 4096
        self._send_incomplete(UPLOAD_ORIGIN)
        self._wait_for_abort_cleanup()

        # Restore the ticket size: the next upload is a normal, complete one.
        self.client.size = 5
        status, headers, body = self._send(
            "POST", "/v1/upload?ticket=ticket-2", body=b"video"
        )
        self.assertEqual(status, 201)
        self.assertCorsVisible(headers)
        self.assertEqual(json.loads(body)["asset"]["filename"], "demo.mp4")

    def test_post_aborted_transfer_does_not_leave_a_partial_file(self):
        self.client.size = 4096
        self._send_incomplete(UPLOAD_ORIGIN)
        self._wait_for_abort_cleanup()

        self.assertEqual(len(self.client.authorize_calls), 1)
        self.assertEqual(list(self.asset_root.iterdir()), [])

    def test_post_stores_the_file_and_answers_201_with_cors_headers(self):
        payload = b"video"

        status, headers, body = self._send(
            "POST", "/v1/upload?ticket=ticket-1", body=payload
        )

        self.assertEqual(status, 201)
        self.assertCorsVisible(headers)
        asset = json.loads(body)["asset"]
        self.assertEqual(asset["size"], len(payload))

        stored = list(self.asset_root.iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), payload)
        self.assertEqual(stored[0].suffix, ".mp4")
        self.assertNotIn(".part", stored[0].name)

        self.assertEqual(len(self.client.complete_calls), 1)
        completed = self.client.complete_calls[0]
        self.assertEqual(completed["ticket"], "ticket-1")
        self.assertEqual(completed["origin"], UPLOAD_ORIGIN)
        self.assertEqual(completed["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(completed["size"], len(payload))
        # The authoritative check happens on the POST, with the reservation.
        self.assertEqual(
            self.client.authorize_calls, [("ticket-1", UPLOAD_ORIGIN, True)]
        )

    def test_browser_round_trip_preflight_then_upload(self):
        preflight_status, preflight_headers, _body = self._send(
            "OPTIONS", "/v1/upload?ticket=ticket-1", headers=PREFLIGHT_HEADERS
        )
        self.assertEqual(preflight_status, 204)
        allowed = {
            method.strip()
            for method in self._header(
                preflight_headers, "Access-Control-Allow-Methods"
            ).split(",")
        }
        self.assertIn("POST", allowed)

        status, headers, body = self._send(
            "POST",
            "/v1/upload?ticket=ticket-1",
            body=b"video",
            headers={"Content-Type": "text/plain;charset=UTF-8"},
        )

        self.assertEqual(status, 201)
        self.assertCorsVisible(headers)
        self.assertIn("asset", json.loads(body))

    def test_every_rejection_path_is_readable_by_the_browser(self):
        """Regression guard: a non-2xx answer without CORS is indistinguishable
        from an agent that is not running."""
        cases = {
            "missing ticket": ("OPTIONS", "/v1/upload", {}),
            "foreign path": ("OPTIONS", "/v1/other?ticket=t", {}),
            "foreign host": ("OPTIONS", "/v1/upload?ticket=t", {"host": "x.example"}),
            "post without ticket": ("POST", "/v1/upload", {}),
        }
        for name, (method, path, extra) in cases.items():
            with self.subTest(case=name):
                _status, headers, _body = self._send(method, path, **extra)
                self.assertEqual(
                    self._header(headers, "Access-Control-Allow-Origin"),
                    UPLOAD_ORIGIN,
                )


if __name__ == "__main__":
    unittest.main()

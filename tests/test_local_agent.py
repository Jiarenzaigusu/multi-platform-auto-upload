from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from local_agent.client import AgentApiError
from local_agent.credentials import AgentConnectionStore
from local_agent.desktop import _connect_when_available
from local_agent import autostart
from local_agent.main import LocalAgentApplication
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

    def test_batch_publish_is_persisted_with_one_state_write(self):
        video = self.paths.media / "demo.mp4"
        video.write_bytes(b"video")
        request = validate_publish_request(
            platform="tmall",
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


class LocalAgentApplicationTests(unittest.TestCase):
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
            def claim(self, _agent_id):
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

    def test_desktop_connection_waits_for_temporary_network_failure(self):
        class RetryingApplication:
            def __init__(self):
                self.attempts = 0

            def connect(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise AgentApiError("网络尚未就绪")

        application = RetryingApplication()
        with patch("local_agent.desktop.time.sleep") as sleep:
            self.assertTrue(_connect_when_available(application))

        self.assertEqual(application.attempts, 2)
        sleep.assert_called_once_with(5)

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
                self.assertTrue(request.dry_run)
                self.assertFalse(request.headless)
                self.assertEqual(upload.await_args.kwargs["session_pool"], session_pool)
                self.assertIn("用户电脑", result["message"])
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

    def test_external_update_script_waits_then_installs_and_restarts(self) -> None:
        from local_agent.updater import _update_script

        script = _update_script()
        self.assertIn("Get-Process -Id $ParentPid", script)
        self.assertIn('"/VERYSILENT"', script)
        self.assertIn("Start-Process -FilePath $AgentExe", script)
        self.assertNotIn("MPAU-Agent.exe", script)

    def test_launch_update_starts_powershell_outside_the_agent_executable(self) -> None:
        from local_agent import updater

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            updater.sys, "frozen", True, create=True
        ), patch.object(
            updater.sys, "executable", str(Path(temp_dir) / "MPAU-Agent.exe")
        ), patch.object(updater.subprocess, "Popen") as popen:
            installer = Path(temp_dir) / "update" / "MPAU-Agent-Setup-0.4.0.exe"
            installer.parent.mkdir()
            installer.write_bytes(b"installer")
            updater.launch_update(Path(temp_dir), installer)

        command = popen.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("-File", command)
        self.assertIn("-ParentPid", command)
        self.assertNotIn("--apply-update", command)

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


if __name__ == "__main__":
    unittest.main()

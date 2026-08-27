from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from webapp.api.main import WebSettings, create_app, server_bind_address
from webapp.api.models import validate_publish_request
from webapp.api.tasks import TaskManager
from webapp.auth import AuthStore
from webapp.workspaces import AppDataPaths, UserWorkspaceRegistry


@dataclass(slots=True)
class _AsgiResponse:
    """Minimal response object used by the dependency-free ASGI test client."""

    status_code: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body or b"null")


class _AsgiClient:
    """Drive the real ASGI middleware stack while retaining response Cookies."""

    def __init__(self, app, *, client_host: str = "testclient") -> None:
        self.app = app
        self.client_host = client_host
        self.cookies: dict[str, str] = {}
        self.last_response_headers: list[tuple[bytes, bytes]] = []

    def get(self, path: str, *, headers: dict[str, str] | None = None):
        return self.request("GET", path, headers=headers)

    def post(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict | None = None,
        files: dict | None = None,
    ):
        return self.request("POST", path, headers=headers, json_body=json, files=files)

    def patch(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict | None = None,
    ):
        return self.request("PATCH", path, headers=headers, json_body=json)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        files: dict | None = None,
        scheme: str = "http",
    ) -> _AsgiResponse:
        request_headers = {"host": "testserver", **(headers or {})}
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["content-type"] = "application/json"
        elif files:
            boundary = "mpau-test-boundary"
            chunks: list[bytes] = []
            for field, (filename, content, content_type) in files.items():
                chunks.extend(
                    [
                        f"--{boundary}\r\n".encode(),
                        (
                            f'Content-Disposition: form-data; name="{field}"; '
                            f'filename="{filename}"\r\n'
                        ).encode(),
                        f"Content-Type: {content_type}\r\n\r\n".encode(),
                        content,
                        b"\r\n",
                    ]
                )
            chunks.append(f"--{boundary}--\r\n".encode())
            body = b"".join(chunks)
            request_headers["content-type"] = f"multipart/form-data; boundary={boundary}"
        if self.cookies:
            request_headers["cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        request_headers["content-length"] = str(len(body))

        messages: list[dict] = []
        request_sent = False

        async def receive():
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        raw_path, _, raw_query = path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": scheme,
            "path": raw_path,
            "raw_path": raw_path.encode("ascii"),
            "query_string": raw_query.encode("ascii"),
            "root_path": "",
            "headers": [
                (name.lower().encode("ascii"), value.encode("utf-8"))
                for name, value in request_headers.items()
            ],
            "client": (self.client_host, 12345),
            "server": ("testserver", 80),
        }
        asyncio.run(self.app(scope, receive, send))

        start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        self.last_response_headers = list(start.get("headers", []))
        for name, value in start.get("headers", []):
            if name.lower() != b"set-cookie":
                continue
            parsed = SimpleCookie()
            parsed.load(value.decode("latin-1"))
            for cookie_name, morsel in parsed.items():
                if morsel["max-age"] == "0":
                    self.cookies.pop(cookie_name, None)
                else:
                    self.cookies[cookie_name] = morsel.value
        response_body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return _AsgiResponse(start["status"], response_body)


class MultiUserApiTests(unittest.TestCase):
    """Exercise authentication and workspace isolation through the real ASGI app."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_paths = AppDataPaths.create(root / "data")

        def manager_factory(store, **kwargs):
            return TaskManager(
                store,
                runner=lambda job: {"message": f"{job['kind']} complete"},
                **kwargs,
            )

        self.registry = UserWorkspaceRegistry(
            self.data_paths,
            user_workers=1,
            global_browser_tasks=1,
            browser_idle_timeout_seconds=0,
            manager_factory=manager_factory,
        )
        settings = WebSettings(
            data_dir=self.data_paths.root,
            frontend_dist_dir=root / "missing-frontend",
            allowed_hosts=("testserver", "10.31.108.221"),
            allowed_origins=("http://10.31.108.221:8788",),
        )
        self.app = create_app(settings, self.registry)
        self.client = _AsgiClient(self.app)

    def tearDown(self) -> None:
        self.registry.close()
        self.temp_dir.cleanup()

    def test_direct_http_listener_uses_deployment_environment(self):
        with patch.dict(
            "os.environ",
            {"MPAU_BIND_HOST": "0.0.0.0", "MPAU_PORT": "8788"},
            clear=False,
        ):
            self.assertEqual(server_bind_address(), ("0.0.0.0", 8788))

    def test_direct_http_listener_rejects_invalid_port(self):
        with patch.dict("os.environ", {"MPAU_PORT": "70000"}, clear=False):
            with self.assertRaisesRegex(ValueError, "1-65535"):
                server_bind_address()

    def test_direct_http_ip_login_preserves_session_and_csrf_flow(self):
        direct_headers = {
            "host": "10.31.108.221:8788",
            "origin": "http://10.31.108.221:8788",
        }
        created = self.client.post(
            "/api/auth/bootstrap",
            headers=direct_headers,
            json={
                "username": "admin",
                "display_name": "Administrator",
                "password": "admin-password-123",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        cookies = [
            value.lower()
            for name, value in self.client.last_response_headers
            if name.lower() == b"set-cookie"
        ]
        self.assertEqual(len(cookies), 2)
        self.assertTrue(all(b"; secure" not in cookie for cookie in cookies))

        session = self.client.get("/api/auth/me", headers=direct_headers)
        self.assertEqual(session.status_code, 200, session.text)
        paired = self.client.post(
            "/api/agent/pairing-code",
            headers={**direct_headers, **self.csrf_headers()},
        )
        self.assertEqual(paired.status_code, 200, paired.text)

    def test_https_login_marks_session_cookies_secure(self):
        created = self.client.request(
            "POST",
            "/api/auth/bootstrap",
            json_body={
                "username": "admin",
                "display_name": "Administrator",
                "password": "admin-password-123",
            },
            scheme="https",
        )
        self.assertEqual(created.status_code, 201, created.text)
        cookies = [
            value.lower()
            for name, value in self.client.last_response_headers
            if name.lower() == b"set-cookie"
        ]
        self.assertEqual(len(cookies), 2)
        self.assertTrue(all(b"; secure" in cookie for cookie in cookies))

    def csrf_headers(self) -> dict[str, str]:
        """Return the double-submit token issued with the current test session."""
        token = self.client.cookies.get("mpau_csrf_v2")
        self.assertTrue(token)
        return {"X-CSRF-Token": token}

    def bootstrap_admin(self) -> dict:
        """Create and sign in the one-time local administrator."""
        response = self.client.post(
            "/api/auth/bootstrap",
            json={
                "username": "admin",
                "display_name": "Administrator",
                "password": "admin-password-123",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_user(
        self,
        username: str,
        *,
        role: str = "operator",
        password: str = "operator-password-123",
    ) -> dict:
        """Provision a user through the administrator-only endpoint."""
        response = self.client.post(
            "/api/admin/users",
            headers=self.csrf_headers(),
            json={
                "username": username,
                "display_name": username.title(),
                "password": password,
                "role": role,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def login(self, username: str, password: str) -> dict:
        """Discard prior Cookies and establish a fresh application session."""
        self.client.cookies.clear()
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def register(
        self,
        username: str,
        *,
        display_name: str = "New Operator",
        password: str = "operator-password-123",
    ) -> dict:
        """Self-register an operator and establish its application session."""
        self.client.cookies.clear()
        response = self.client.post(
            "/api/auth/register",
            json={
                "username": username,
                "display_name": display_name,
                "password": password,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def wait_for_jobs(self) -> list[dict]:
        """Wait briefly for deterministic in-memory runners to finish."""
        for _ in range(50):
            response = self.client.get("/api/jobs")
            self.assertEqual(response.status_code, 200, response.text)
            jobs = response.json()["jobs"]
            if jobs and jobs[0]["status"] == "succeeded":
                return jobs
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_bootstrap_session_and_csrf_protection(self):
        status = self.client.get("/api/auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["setup_required"])

        admin = self.bootstrap_admin()
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(self.client.cookies.get("mpau_session_v2"))
        self.assertTrue(self.client.cookies.get("mpau_csrf_v2"))

        rejected = self.client.post("/api/accounts/tmall/shop1/check")
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post(
            "/api/accounts/tmall/shop1/check", headers=self.csrf_headers()
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(self.wait_for_jobs()[0]["account"], "shop1")

        second_bootstrap = self.client.post(
            "/api/auth/bootstrap",
            json={
                "username": "otheradmin",
                "display_name": "Other",
                "password": "other-password-123",
            },
        )
        self.assertEqual(second_bootstrap.status_code, 409)

    def test_remote_bootstrap_is_blocked_unless_deployment_allows_it(self):
        remote_client = _AsgiClient(self.app, client_host="10.0.0.2")
        blocked = remote_client.post(
            "/api/auth/bootstrap",
            json={
                "username": "remoteadmin",
                "display_name": "Remote Admin",
                "password": "admin-password-123",
            },
        )
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertEqual(self.app.state.auth_service.store.user_count(), 0)

        self.registry.close()
        settings = WebSettings(
            data_dir=self.data_paths.root,
            frontend_dist_dir=Path(self.temp_dir.name) / "missing-frontend",
            allow_remote_bootstrap=True,
        )
        self.registry = UserWorkspaceRegistry(
            self.data_paths,
            user_workers=1,
            global_browser_tasks=1,
            browser_idle_timeout_seconds=0,
            manager_factory=lambda store, **kwargs: TaskManager(
                store,
                runner=lambda job: {"message": f"{job['kind']} complete"},
                **kwargs,
            ),
        )
        allowed_app = create_app(settings, self.registry)
        allowed_client = _AsgiClient(allowed_app, client_host="10.0.0.2")
        created = allowed_client.post(
            "/api/auth/bootstrap",
            json={
                "username": "remoteadmin",
                "display_name": "Remote Admin",
                "password": "admin-password-123",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["role"], "admin")

    def test_public_registration_creates_only_operators_and_signs_them_in(self):
        before_setup = self.client.post(
            "/api/auth/register",
            json={
                "username": "tooearly",
                "display_name": "Too Early",
                "password": "operator-password-123",
            },
        )
        self.assertEqual(before_setup.status_code, 409, before_setup.text)
        self.assertEqual(self.app.state.auth_service.store.user_count(), 0)

        self.bootstrap_admin()
        operator = self.register("selfservice")
        self.assertEqual(operator["role"], "operator")
        self.assertTrue(self.client.cookies.get("mpau_session_v2"))
        self.assertTrue(self.client.cookies.get("mpau_csrf_v2"))
        self.assertEqual(
            self.client.get("/api/auth/me").json()["id"], operator["id"]
        )
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)

        duplicate = self.client.post(
            "/api/auth/register",
            json={
                "username": "selfservice",
                "display_name": "Duplicate",
                "password": "operator-password-456",
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        requested_admin = self.client.post(
            "/api/auth/register",
            json={
                "username": "rogueadmin",
                "display_name": "Rogue Admin",
                "password": "operator-password-789",
                "role": "admin",
            },
        )
        self.assertEqual(requested_admin.status_code, 422, requested_admin.text)

        admin = self.login("admin", "admin-password-123")
        self.assertEqual(admin["role"], "admin")

    def test_jobs_cookies_and_media_are_isolated_by_user_id(self):
        admin = self.bootstrap_admin()
        operator = self.create_user("operator")

        first_job = self.client.post(
            "/api/accounts/tmall/shared-shop/check", headers=self.csrf_headers()
        )
        self.assertEqual(first_job.status_code, 202)
        self.wait_for_jobs()
        admin_workspace = self.registry.get(admin["id"])
        admin_cookie = admin_workspace.paths.cookie_file("tmall", "shared-shop")
        admin_cookie.write_text('{"owner":"admin"}', encoding="utf-8")

        self.login("operator", "operator-password-123")
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        second_job = self.client.post(
            "/api/accounts/tmall/shared-shop/check", headers=self.csrf_headers()
        )
        self.assertEqual(second_job.status_code, 202)
        self.wait_for_jobs()
        operator_workspace = self.registry.get(operator["id"])
        operator_cookie = operator_workspace.paths.cookie_file("tmall", "shared-shop")
        operator_cookie.write_text('{"owner":"operator"}', encoding="utf-8")

        self.assertNotEqual(admin_workspace.paths.root, operator_workspace.paths.root)
        self.assertNotEqual(admin_cookie, operator_cookie)
        self.assertEqual(admin_cookie.read_text(encoding="utf-8"), '{"owner":"admin"}')
        self.assertEqual(
            operator_cookie.read_text(encoding="utf-8"), '{"owner":"operator"}'
        )

        uploaded = self.client.post(
            "/api/media",
            headers=self.csrf_headers(),
            files={"files": ("operator-video.mp4", b"video", "video/mp4")},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertTrue(
            (operator_workspace.paths.media / "operator-video.mp4").is_file()
        )
        self.assertFalse(
            (admin_workspace.paths.media / "operator-video.mp4").exists()
        )

    def test_roles_user_lifecycle_and_last_admin_guard(self):
        admin = self.bootstrap_admin()
        operator = self.create_user("operator")

        rejected_viewer = self.client.post(
            "/api/admin/users",
            headers=self.csrf_headers(),
            json={
                "username": "viewer",
                "display_name": "Removed Role",
                "password": "viewer-password-123",
                "role": "viewer",
            },
        )
        self.assertEqual(rejected_viewer.status_code, 422)

        last_admin = self.client.patch(
            f"/api/admin/users/{admin['id']}",
            headers=self.csrf_headers(),
            json={"status": "disabled"},
        )
        self.assertEqual(last_admin.status_code, 409)

        self.login("operator", "operator-password-123")
        self.assertEqual(self.client.get("/api/jobs").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)

        self.login("admin", "admin-password-123")
        reset = self.client.post(
            f"/api/admin/users/{operator['id']}/reset-password",
            headers=self.csrf_headers(),
            json={"password": "replacement-password-123"},
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.login("operator", "replacement-password-123")
        self.assertEqual(self.client.get("/api/auth/me").json()["id"], operator["id"])

    def test_admin_can_delete_operator_but_not_current_account(self):
        admin = self.bootstrap_admin()
        operator = self.create_user("operator")
        operator_workspace = self.registry.get(operator["id"])
        operator_workspace.store.remember_account("tmall", "shop1")
        operator_workspace.paths.cookie_file("tmall", "shop1").write_text(
            '{"cookie":"operator"}', encoding="utf-8"
        )
        (operator_workspace.paths.media / "operator.mp4").write_bytes(b"video")
        upload_dir = operator_workspace.paths.uploads / ("a" * 32)
        upload_dir.mkdir()
        (upload_dir / "cover.png").write_bytes(b"cover")
        (operator_workspace.paths.job_logs / "job.log").write_text(
            "operator job", encoding="utf-8"
        )
        (operator_workspace.paths.platform_logs / "platform.log").write_text(
            "operator platform", encoding="utf-8"
        )
        (operator_workspace.paths.secrets / "llm-adapter-credentials.json").write_text(
            '{"secret":"operator"}', encoding="utf-8"
        )
        operator_root = operator_workspace.paths.root

        operator_client = _AsgiClient(self.app)
        login = operator_client.post(
            "/api/auth/login",
            json={
                "username": "operator",
                "password": "operator-password-123",
            },
        )
        self.assertEqual(login.status_code, 200, login.text)

        delete_self = self.client.request(
            "DELETE",
            f"/api/admin/users/{admin['id']}",
            headers=self.csrf_headers(),
        )
        self.assertEqual(delete_self.status_code, 409)
        self.assertIn("不能删除当前登录账号", delete_self.text)

        deleted = self.client.request(
            "DELETE",
            f"/api/admin/users/{operator['id']}",
            headers=self.csrf_headers(),
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        users = self.client.get("/api/admin/users").json()
        self.assertNotIn(operator["id"], [user["id"] for user in users])
        self.assertEqual(operator_client.get("/api/auth/me").status_code, 401)
        self.assertFalse(operator_root.exists())

        connection = sqlite3.connect(self.data_paths.auth_database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM users WHERE id = ?",
                    (operator["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                    (operator["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_devices WHERE user_id = ?",
                    (operator["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_pairing_codes WHERE user_id = ?",
                    (operator["id"],),
                ).fetchone()[0],
                0,
            )
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE user_id = ? OR resource_id = ?",
                (operator["id"], operator["id"]),
            ).fetchone()[0]
            self.assertEqual(audit_count, 0)
        finally:
            connection.close()

        relogin = self.client.post(
            "/api/auth/login",
            json={
                "username": "operator",
                "password": "operator-password-123",
            },
        )
        self.assertEqual(relogin.status_code, 401)


class LocalAgentApiTests(unittest.TestCase):
    """Exercise the cloud broker without constructing a server-side browser."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_paths = AppDataPaths.create(root / "data")
        self.registry = UserWorkspaceRegistry(
            self.data_paths,
            user_workers=1,
            global_browser_tasks=10,
            browser_idle_timeout_seconds=300,
        )
        self.app = create_app(
            WebSettings(
                data_dir=self.data_paths.root,
                frontend_dist_dir=root / "missing-frontend",
            ),
            self.registry,
        )
        self.client = _AsgiClient(self.app)
        response = self.client.post(
            "/api/auth/bootstrap",
            json={
                "username": "admin",
                "display_name": "Administrator",
                "password": "admin-password-123",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.user = response.json()
        self.agent_token = ""

    def tearDown(self) -> None:
        self.registry.close()
        self.temp_dir.cleanup()

    def csrf_headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies["mpau_csrf_v2"]}

    def post_json(self, path: str, payload: dict) -> _AsgiResponse:
        return self.client.post(path, headers=self.csrf_headers(), json=payload)

    def agent_post_json(self, path: str, payload: dict) -> _AsgiResponse:
        token_only = _AsgiClient(self.app)
        return token_only.post(
            path,
            headers={"Authorization": f"Bearer {self.agent_token}"},
            json=payload,
        )

    def pair_agent(self) -> dict:
        pairing = self.post_json("/api/agent/pairing-code", {})
        self.assertEqual(pairing.status_code, 200, pairing.text)
        pairing_code = pairing.json()["pairing_code"]
        response = self.client.post(
            "/api/agent/pair",
            json={
                "pairing_code": pairing_code,
                "agent_id": "b" * 32,
                "device_name": "Operator-PC",
                "system": "Windows 11",
                "version": "test",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.agent_token = response.json()["agent_token"]
        self.assertEqual(response.json()["user"]["id"], self.user["id"])
        return response.json() | {"_pairing_code": pairing_code}

    def connect_agent(self) -> dict:
        if not self.agent_token:
            self.pair_agent()
        response = self.agent_post_json(
            "/api/agent/connect",
            {
                "agent_id": "b" * 32,
                "device_name": "Operator-PC",
                "system": "Windows 11",
                "version": "test",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_local_agent_claim_complete_and_cookie_deletion_workflow(self):
        workspace = self.registry.get(self.user["id"])
        self.assertTrue(workspace.task_manager.remote_execution)
        self.assertIsNone(workspace.task_manager.browser_runtime)

        self.connect_agent()
        status = self.client.get("/api/agent/status")
        self.assertTrue(status.json()["online"])

        created = self.client.post(
            "/api/accounts/tmall/shop1/login", headers=self.csrf_headers()
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["job"]["id"]
        self.assertEqual(workspace.store.get_job(job_id)["status"], "queued")

        claimed = self.agent_post_json(
            "/api/agent/claim", {"agent_id": "b" * 32}
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["job"]["id"], job_id)
        self.assertNotIn("video_path", claimed.json()["job"]["payload"])

        completed = self.agent_post_json(
            f"/api/agent/jobs/{job_id}/complete",
            {
                "agent_id": "b" * 32,
                "status": "succeeded",
                "message": "local login complete",
                "result": {},
                "logs": ["local edge log"],
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(workspace.store.get_job(job_id)["status"], "succeeded")

        delete = self.client.request(
            "DELETE",
            "/api/accounts/tmall/shop1",
            headers=self.csrf_headers(),
        )
        self.assertEqual(delete.status_code, 200, delete.text)
        self.assertTrue(delete.json()["deletion_pending"])
        delete_job_id = delete.json()["job"]["id"]
        claimed_delete = self.agent_post_json(
            "/api/agent/claim", {"agent_id": "b" * 32}
        )
        self.assertEqual(claimed_delete.json()["job"]["id"], delete_job_id)
        self.agent_post_json(
            f"/api/agent/jobs/{delete_job_id}/complete",
            {
                "agent_id": "b" * 32,
                "status": "succeeded",
                "message": "cookie deleted locally",
                "result": {"cookie_deleted": True},
            },
        )
        self.assertEqual(workspace.store.list_accounts(), [])

    def test_tmall_cover_is_downloadable_only_by_the_claimed_agent(self):
        workspace = self.registry.get(self.user["id"])
        workspace.task_manager.start()
        upload_dir = workspace.paths.uploads / ("d" * 32)
        upload_dir.mkdir()
        video = upload_dir / "demo.mp4"
        video.write_bytes(b"video")
        cover = upload_dir / "cover-test.png"
        cover.write_bytes(b"cover")
        request = validate_publish_request(
            platform="tmall",
            cover_ratio="3:4",
            account="shop1",
            video_path=video,
            cover_image_path=cover,
            original_filename=video.name,
            title="自定义封面传输测试",
            managed_upload=True,
        )
        job = workspace.task_manager.submit_publish_task(request)

        self.connect_agent()
        claimed = self.agent_post_json(
            "/api/agent/claim", {"agent_id": "b" * 32}
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        claimed_job = claimed.json()["job"]
        self.assertEqual(claimed_job["id"], job["id"])
        self.assertNotIn("video_path", claimed_job["payload"])
        self.assertNotIn("cover_image_path", claimed_job["payload"])
        self.assertEqual(
            claimed_job["payload"]["cover_image_filename"], cover.name
        )
        self.assertEqual(
            claimed_job["cover_image_download_url"],
            f"/api/agent/jobs/{job['id']}/cover-image",
        )

        token_only = _AsgiClient(self.app)
        headers = {"Authorization": f"Bearer {self.agent_token}"}
        query = f"?agent_id={'b' * 32}"
        video_response = token_only.get(
            f"/api/agent/jobs/{job['id']}/video{query}", headers=headers
        )
        cover_response = token_only.get(
            f"/api/agent/jobs/{job['id']}/cover-image{query}", headers=headers
        )
        self.assertEqual(video_response.status_code, 200, video_response.text)
        self.assertEqual(video_response.body, b"video")
        self.assertEqual(cover_response.status_code, 200, cover_response.text)
        self.assertEqual(cover_response.body, b"cover")

        browser_session_only = self.client.get(
            f"/api/agent/jobs/{job['id']}/cover-image{query}"
        )
        self.assertEqual(browser_session_only.status_code, 401)

        completed = self.agent_post_json(
            f"/api/agent/jobs/{job['id']}/complete",
            {
                "agent_id": "b" * 32,
                "status": "succeeded",
                "message": "cover uploaded",
                "result": {},
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertFalse(upload_dir.exists())

    def test_pairing_code_is_single_use_and_token_is_device_bound(self):
        paired = self.pair_agent()
        reused = self.client.post(
            "/api/agent/pair",
            json={
                "pairing_code": paired["_pairing_code"],
                "agent_id": "c" * 32,
                "device_name": "Other-PC",
                "system": "Windows 11",
                "version": "test",
            },
        )
        self.assertEqual(reused.status_code, 401)

        mismatch = self.agent_post_json(
            "/api/agent/connect",
            {
                "agent_id": "c" * 32,
                "device_name": "Other-PC",
                "system": "Windows 11",
                "version": "test",
            },
        )
        self.assertEqual(mismatch.status_code, 403)

        token_only = _AsgiClient(self.app)
        ordinary_api = token_only.get(
            "/api/jobs",
            headers={"Authorization": f"Bearer {paired['agent_token']}"},
        )
        self.assertEqual(ordinary_api.status_code, 401)
        pairing_code = token_only.post(
            "/api/agent/pairing-code",
            headers={"Authorization": f"Bearer {paired['agent_token']}"},
            json={},
        )
        self.assertEqual(pairing_code.status_code, 401)

        revoked = self.client.request(
            "DELETE",
            f"/api/agent/devices/{'b' * 32}",
            headers=self.csrf_headers(),
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        rejected = self.agent_post_json(
            "/api/agent/connect",
            {
                "agent_id": "b" * 32,
                "device_name": "Operator-PC",
                "system": "Windows 11",
                "version": "test",
            },
        )
        self.assertEqual(rejected.status_code, 401)

    def test_revoking_user_sessions_also_revokes_agent_device(self):
        self.pair_agent()
        revoked = self.client.post(
            f"/api/admin/users/{self.user['id']}/revoke-sessions",
            headers=self.csrf_headers(),
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        rejected = self.agent_post_json(
            "/api/agent/connect",
            {
                "agent_id": "b" * 32,
                "device_name": "Operator-PC",
                "system": "Windows 11",
                "version": "test",
            },
        )
        self.assertEqual(rejected.status_code, 401)


class RemovedViewerMigrationTests(unittest.TestCase):
    """Migrate former viewer accounts without granting publishing access."""

    def test_existing_viewer_is_disabled_and_database_rejects_the_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "auth.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
                    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'locked')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            user_id = "a" * 32
            connection.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, role, status,
                    created_at, updated_at
                ) VALUES (?, 'reader', 'Reader', 'hash', 'viewer', 'active', ?, ?)
                """,
                (user_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_hash, csrf_hash, created_at, expires_at,
                    ip_address, user_agent, revoked_at
                ) VALUES ('session-1', ?, 'token-hash', 'csrf-hash', ?, ?, '', '', NULL)
                """,
                (
                    user_id,
                    "2026-01-01T00:00:00+00:00",
                    "2099-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()
            connection.close()

            store = AuthStore(database)
            migrated = store.get_user(user_id)
            self.assertIsNotNone(migrated)
            self.assertEqual(migrated.role, "operator")
            self.assertEqual(migrated.status, "disabled")

            connection = sqlite3.connect(database)
            try:
                schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
                ).fetchone()[0]
                self.assertNotIn("viewer", schema)
                revoked_at = connection.execute(
                    "SELECT revoked_at FROM sessions WHERE id = 'session-1'"
                ).fetchone()[0]
                self.assertIsNotNone(revoked_at)
                action = connection.execute(
                    "SELECT action FROM audit_logs WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                self.assertEqual(action, "remove_viewer_role")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO users (
                            id, username, display_name, password_hash, role, status,
                            created_at, updated_at
                        ) VALUES (?, 'reader2', 'Reader 2', 'hash', 'viewer', 'active', ?, ?)
                        """,
                        (
                            "b" * 32,
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
            finally:
                connection.close()


class MultiUserCapacityTests(unittest.TestCase):
    """Verify the production concurrency profile across isolated workspaces."""

    def test_ten_users_run_while_excess_browser_tasks_wait_for_a_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_paths = AppDataPaths.create(Path(temp_dir) / "data")
            counter_lock = threading.Lock()
            ten_started = threading.Event()
            release = threading.Event()
            active = 0
            peak = 0

            def runner(_job):
                nonlocal active, peak
                with counter_lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 10:
                        ten_started.set()
                try:
                    release.wait(timeout=10)
                    return {"message": "complete"}
                finally:
                    with counter_lock:
                        active -= 1

            def manager_factory(store, **kwargs):
                return TaskManager(store, runner=runner, **kwargs)

            registry = UserWorkspaceRegistry(
                data_paths,
                user_workers=1,
                global_browser_tasks=10,
                browser_idle_timeout_seconds=300,
                manager_factory=manager_factory,
            )
            submitted: list[tuple[TaskManager, str]] = []
            try:
                for number in range(1, 13):
                    workspace = registry.get(f"{number:032x}")
                    job = workspace.task_manager.submit_account_task(
                        kind="check",
                        platform="tmall",
                        account="shared-shop",
                        headed=False,
                    )
                    submitted.append((workspace.task_manager, job["id"]))

                self.assertTrue(ten_started.wait(timeout=3))
                time.sleep(0.05)
                with counter_lock:
                    self.assertEqual(active, 10)
                    self.assertEqual(peak, 10)

                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if all(
                        manager.store.get_job(job_id)["status"] == "succeeded"
                        for manager, job_id in submitted
                    ):
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    all(
                        manager.store.get_job(job_id)["status"] == "succeeded"
                        for manager, job_id in submitted
                    )
                )
                self.assertEqual(peak, 10)
            finally:
                release.set()
                registry.close()


if __name__ == "__main__":
    unittest.main()

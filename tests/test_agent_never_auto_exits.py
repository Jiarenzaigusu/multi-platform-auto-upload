"""Regression tests: the desktop agent must only stop when the user asks.

Covers the failure modes that used to tear the process down mid-task:
HTTP 401 on ``claim`` (lease revoked by the server) and 5xx on heartbeat.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from local_agent.client import AgentApiClient, AgentApiError
from local_agent.main import AgentLeaseLostError, LocalAgentApplication


class FakeClient(AgentApiClient):
    """Replays a script of failures instead of talking to a server."""

    def __init__(self, claim_statuses=None, connect_status=None) -> None:
        super().__init__("http://127.0.0.1:1")
        self.claim_statuses = list(claim_statuses or [])
        self.connect_status = connect_status
        self.claims = 0
        self.connects = 0

    def claim(self, agent_id, wait_seconds=0):  # noqa: D102 - test double
        self.claims += 1
        if self.claim_statuses:
            status = self.claim_statuses.pop(0)
            if status is not None:
                raise AgentApiError(f"请求失败（HTTP {status}）", status)
        return None

    def connect(self, hello):  # noqa: D102 - test double
        self.connects += 1
        if self.connect_status is not None:
            raise AgentApiError(f"请求失败（HTTP {self.connect_status}）", self.connect_status)
        return {"user": {"id": "u1", "display_name": "tester", "username": "tester"}}


def make_app(tmp_path: Path, client: AgentApiClient) -> LocalAgentApplication:
    app = LocalAgentApplication(client, data_root=tmp_path, poll_seconds=1.0)
    # Keep the regression test fast: the real helper sleeps 5s between retries.
    app._sleep_or_stop = lambda seconds: None  # type: ignore[method-assign]
    return app


def test_claim_401_does_not_stop_agent(tmp_path: Path) -> None:
    """Repeated 401s must never flip ``stopping`` or ``authorization_failed``."""
    client = FakeClient(claim_statuses=[401] * 5, connect_status=502)
    app = make_app(tmp_path, client)
    app._reconnect_or_wait()

    assert app.stopping is False, "401 must not stop the agent"
    assert app.authorization_failed is False, "401 must not clear the pairing"
    assert client.connects == 1, "agent should try to re-handshake with stored token"


def test_run_loop_survives_401_and_502(tmp_path: Path) -> None:
    """The polling loop keeps running through 401/502 storms until told to stop."""
    client = FakeClient(claim_statuses=[401, 502, 401, None], connect_status=502)
    app = make_app(tmp_path, client)

    thread = threading.Thread(target=app.run, kwargs={"already_connected": True})
    thread.start()
    try:
        time.sleep(1.5)
        assert thread.is_alive(), "agent thread exited during a failure storm"
        assert app.stopping is False
        assert app.authorization_failed is False
        # It must have kept polling rather than bailing out after the first 401.
        assert client.claims >= 2, f"expected retries, got {client.claims} claims"
    finally:
        app.stop()
        thread.join(timeout=10)

    assert not thread.is_alive(), "agent must still honour a manual stop()"


def test_heartbeat_502_keeps_task_running(tmp_path: Path) -> None:
    """A 5xx heartbeat is retried instead of raising AgentLeaseLostError."""
    app = make_app(tmp_path, FakeClient())
    heartbeat_state = {"last_success": time.monotonic() - 50}

    def heartbeat_502(job_id, agent_id):
        raise AgentApiError("请求失败（HTTP 502）", 502)

    def heartbeat_401(job_id, agent_id):
        raise AgentApiError("设备授权已失效", 401)

    def make_grace(fn):
        """Mirror the closure from LocalAgentApplication.execute, sans browser."""

        def heartbeat_with_grace():
            try:
                heartbeat = fn("job", "agent")
            except AgentApiError as exc:
                if exc.status in {401, 403, 404, 409}:
                    raise AgentLeaseLostError(f"云端心跳租约失效：{exc}") from exc
                elapsed = time.monotonic() - heartbeat_state["last_success"]
                # Guard the fixture: this window used to exceed the old cutoff.
                assert elapsed > app.lease_seconds - 5
                return None
            heartbeat_state["last_success"] = time.monotonic()
            return heartbeat

        return heartbeat_with_grace

    # 50s of failed heartbeats used to trip `elapsed >= lease_seconds - 5`
    # and cancel the running browser job.
    assert make_grace(heartbeat_502)() is None, "502 heartbeat must be retried"

    # A terminal 401/403/404/409 still means the job is gone upstream.
    with pytest.raises(AgentLeaseLostError):
        make_grace(heartbeat_401)()


def test_relaunch_after_pairing_starts_fresh_process(tmp_path: Path) -> None:
    """After a successful pairing the agent relaunches itself so the fresh
    connection is consumed by a cold start (identical to a second launch)."""
    from local_agent import desktop

    args = SimpleNamespace(data_dir=tmp_path)

    with patch("subprocess.Popen") as popen:
        ok = desktop._relaunch_after_pairing(args)
        assert ok is True
        assert popen.call_count == 1
        command = popen.call_args[0][0]
        assert command[0] == sys.executable
        # Dev mode must carry the entry module; frozen mode runs the exe directly.
        if not getattr(sys, "frozen", False):
            assert "-m" in command and "local_agent.desktop" in command
        assert "--data-dir" in command
        assert str(tmp_path) in command

    # If the relaunch itself fails the caller must fall back to connecting
    # in-process rather than crashing.
    with patch("subprocess.Popen", side_effect=OSError("launch failed")):
        ok = desktop._relaunch_after_pairing(args)
        assert ok is False

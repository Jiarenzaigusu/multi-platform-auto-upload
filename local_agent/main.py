from __future__ import annotations

import argparse
import asyncio
import platform
import shutil
import signal
import socket
import sys
import time
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from urllib.parse import urlsplit

from local_agent import __version__
from local_agent.client import AgentApiClient, AgentApiError
from local_agent.credentials import AgentConnectionStore
from local_agent.paths import (
    default_data_root,
    load_or_create_agent_id,
    secure_directory,
    user_paths,
)
from local_agent.runner import AgentJobRunner
from uploader.errors import PublishResultUncertainError
from utils.files import validate_media_filename


class AgentJobCancelledError(RuntimeError):
    pass


class AgentLeaseLostError(RuntimeError):
    pass


class LocalAgentApplication:
    def __init__(
        self,
        client: AgentApiClient,
        *,
        data_root: Path,
        poll_seconds: float,
    ) -> None:
        self.client = client
        self.data_root = secure_directory(data_root)
        self.agent_id = load_or_create_agent_id(self.data_root)
        self.poll_seconds = max(1.0, poll_seconds)
        self.lease_seconds = 45.0
        self.stopping = False
        self.authorization_failed = False
        self.runner: AgentJobRunner | None = None
        self.hello = {
            "agent_id": self.agent_id,
            "device_name": socket.gethostname() or "Local PC",
            "system": platform.platform()[:200],
            "version": __version__,
        }

    def stop(self, *_args) -> None:
        self.stopping = True

    def connect(self) -> None:
        response = self.client.connect(self.hello)
        user = response["user"]
        paths = user_paths(self.data_root, user["id"])
        if self.runner is not None and self.runner.user_id != user["id"]:
            self.runner.shutdown()
            self.runner = None
        if self.runner is None:
            self.runner = AgentJobRunner(user["id"], paths)
        self.poll_seconds = max(1.0, float(response.get("poll_seconds", self.poll_seconds)))
        self.lease_seconds = max(30.0, float(response.get("lease_seconds", 45)))
        print(
            f"本地代理已连接：{user['display_name']} ({user['username']})，"
            f"设备 {self.hello['device_name']}"
        )
        print("发布任务将在这台电脑上启动 Microsoft Edge。按 Ctrl+C 停止代理。")

    def run(self, *, already_connected: bool = False) -> None:
        if not already_connected:
            self.connect()
        while not self.stopping:
            try:
                job = self.client.claim(self.agent_id)
                if job is None:
                    time.sleep(self.poll_seconds)
                    continue
                self.execute(job)
            except AgentApiError as exc:
                if exc.status == 401:
                    self.authorization_failed = True
                    self.stopping = True
                    print(
                        "设备授权已失效，需要重新配对本地执行助手",
                        file=sys.stderr,
                    )
                    break
                print(f"代理连接异常：{exc}，5 秒后重试", file=sys.stderr)
                time.sleep(5)
                try:
                    self.client.connect(self.hello)
                except AgentApiError:
                    continue
            except KeyboardInterrupt:
                self.stopping = True
        if self.runner is not None:
            self.runner.shutdown()

    def execute(self, job: dict) -> None:
        assert self.runner is not None
        job_id = job["id"]
        label = {"tmall": "天猫", "jd": "京东"}.get(job["platform"], job["platform"])
        print(f"领取任务：{label} / {job['account']} / {job['kind']} / {job_id}")
        video_path: Path | None = None
        download_dir: Path | None = None
        future = None
        status = "failed"
        message = "本地代理任务失败"
        error = ""
        result: dict = {}
        cancellation_reason = ""
        heartbeat_state = {"last_success": time.monotonic()}

        def heartbeat_with_grace() -> dict | None:
            try:
                heartbeat = self.client.heartbeat(job_id, self.agent_id)
            except AgentApiError as exc:
                elapsed = time.monotonic() - heartbeat_state["last_success"]
                terminal_error = exc.status in {401, 403, 404, 409}
                if terminal_error or elapsed >= self.lease_seconds - 5:
                    raise AgentLeaseLostError(f"云端心跳租约失效：{exc}") from exc
                print(
                    f"云端心跳暂时失败：{exc}，将在租约有效期内继续重试",
                    file=sys.stderr,
                )
                return None
            heartbeat_state["last_success"] = time.monotonic()
            if heartbeat.get("cancel_requested"):
                raise AgentJobCancelledError("用户请求中断任务")
            return heartbeat

        try:
            if job["kind"] == "publish":
                original_name = validate_media_filename(
                    job.get("payload", {}).get("original_filename") or "video.mp4"
                )
                download_dir = secure_directory(self.runner.paths.uploads / job_id)
                video_path = download_dir / original_name
                print(f"正在下载任务视频：{original_name}")
                self.client.download_video(
                    job_id,
                    self.agent_id,
                    video_path,
                    progress=heartbeat_with_grace,
                )
                heartbeat_with_grace()
                if not video_path.is_file() or video_path.stat().st_size == 0:
                    raise RuntimeError("任务视频下载为空")

            future = self.runner.submit(job, video_path)
            while not future.done():
                try:
                    result = future.result(timeout=10)
                    break
                except FutureTimeoutError:
                    try:
                        heartbeat_with_grace()
                    except AgentJobCancelledError as exc:
                        cancellation_reason = str(exc)
                        print("收到中断请求，正在停止本地浏览器任务")
                        self.runner.cancel(job_id)
                    except AgentLeaseLostError as exc:
                        cancellation_reason = str(exc)
                        print(
                            f"{cancellation_reason}，正在停止本地浏览器任务",
                            file=sys.stderr,
                        )
                        self.runner.cancel(job_id)
            if future.done() and not result:
                result = future.result()
            status = "succeeded"
            message = result.get("message", "本地代理任务已完成")
        except PublishResultUncertainError as exc:
            status = "uncertain"
            message = "平台提交结果无法确认，请先到平台后台核对，确认前不要重试"
            error = str(exc)
        except AgentJobCancelledError as exc:
            status = "cancelled"
            message = "任务已按用户请求在本地执行前中断"
            error = str(exc)
        except AgentLeaseLostError as exc:
            status = "cancelled"
            message = "云端连接中断，本地任务已停止"
            error = str(exc)
        except (FutureCancelledError, asyncio.CancelledError):
            status = "cancelled"
            message = (
                "云端连接中断，本地浏览器任务已停止"
                if cancellation_reason.startswith("云端")
                else "本地浏览器任务已中断"
            )
            error = cancellation_reason
        except Exception as exc:
            status = "failed"
            message = "本地代理任务失败，请查看任务日志或本机失败截图"
            error = str(exc)
        finally:
            try:
                logs = self.runner.finish_logs(job_id)
            except Exception as exc:
                logs = [f"读取本地任务日志失败：{exc}"]
            if download_dir is not None and download_dir.exists():
                for attempt in range(3):
                    try:
                        shutil.rmtree(download_dir)
                        break
                    except OSError as exc:
                        if attempt == 2:
                            logs.append(f"本地临时视频清理失败：{exc}")
                        else:
                            time.sleep(0.5 * (attempt + 1))

        for attempt in range(3):
            try:
                self.client.complete(
                    job_id,
                    agent_id=self.agent_id,
                    status=status,
                    message=message,
                    error=error,
                    result=result,
                    logs=logs,
                )
                print(f"任务结束：{status} - {message}")
                break
            except AgentApiError as exc:
                retryable = exc.status == 0 or exc.status >= 500
                if retryable and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                print(
                    f"任务已在本机结束，但结果无法回传：{exc}。请勿直接重试发布，先在平台后台核对。",
                    file=sys.stderr,
                )
                break


def _server_url(value: str, *, allow_http: bool) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    local_host = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("服务地址格式无效")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and (local_host or allow_http)):
        raise ValueError("本地代理必须连接 HTTPS；本机开发地址可使用 HTTP")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在用户电脑上执行 MPAU 天猫/京东 Edge 自动化任务"
    )
    parser.add_argument("--server", help="发布台地址，例如 https://mpau.example.com")
    parser.add_argument("--pair-code", help="网页生成的一次性设备配对码")
    parser.add_argument("--data-dir", type=Path, default=default_data_root())
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="允许连接非本机 HTTP，仅限可信开发网络",
    )
    return parser


def run() -> None:
    args = build_parser().parse_args()
    connection_store = AgentConnectionStore(args.data_dir)
    try:
        stored = connection_store.load()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    selected_server = args.server or (stored.server_url if stored else "")
    if not selected_server:
        raise SystemExit("尚未配对，请运行 MPAU 本地执行助手并输入网页生成的配对码")
    try:
        server = _server_url(selected_server, allow_http=args.allow_http)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    token = stored.agent_token if stored and stored.server_url == server else ""
    client = AgentApiClient(server, token)
    application = LocalAgentApplication(
        client,
        data_root=args.data_dir,
        poll_seconds=args.poll_seconds,
    )
    if args.pair_code:
        try:
            paired = client.pair(application.hello, args.pair_code)
        except AgentApiError as exc:
            raise SystemExit(str(exc)) from exc
        connection_store.save(
            server_url=server,
            agent_token=paired["agent_token"],
            user=paired["user"],
            expires_at=paired["expires_at"],
        )
    elif not token:
        raise SystemExit("该服务器尚未配对，请提供网页生成的 --pair-code")
    signal.signal(signal.SIGINT, application.stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, application.stop)
    try:
        application.run()
    except AgentApiError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    run()

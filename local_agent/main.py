from __future__ import annotations

import argparse
import asyncio
import platform
import re
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
from local_agent.local_upload import LocalUploadServer
from local_agent.paths import (
    default_data_root,
    load_or_create_agent_id,
    secure_directory,
    user_paths,
)
from local_agent.runner import AgentJobRunner
from uploader.errors import PublishResultUncertainError
from utils.files import validate_cover_image_filename, validate_media_filename
from webapp.api.models import JD_ARTICLE_IMAGE_EXTENSIONS, MAX_SOCIAL_ARTICLE_IMAGES, SUPPORTED_COVER_IMAGE_EXTENSIONS

_LOCAL_ASSET_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _local_asset_path(asset_root: Path, asset: object, expected_kind: str) -> Path:
    if not isinstance(asset, dict) or asset.get("kind") != expected_kind:
        raise RuntimeError("任务中的本机素材类型无效")
    asset_id = str(asset.get("asset_id") or "")
    if not _LOCAL_ASSET_ID_PATTERN.fullmatch(asset_id):
        raise RuntimeError("任务中的本机素材 ID 无效")
    filename = Path(str(asset.get("filename") or "")).name
    if not filename:
        raise RuntimeError("任务中的本机素材文件名无效")
    path = asset_root / f"{asset_id}{Path(filename).suffix.lower()}"
    try:
        expected_size = int(asset.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("任务中的本机素材大小无效") from exc
    if not path.is_file() or expected_size <= 0 or path.stat().st_size != expected_size:
        raise RuntimeError(f"本机素材不存在或不完整：{filename}")
    return path


class AgentJobCancelledError(RuntimeError):
    pass


class AgentLeaseLostError(RuntimeError):
    pass


def _article_images_from_folder(
    raw_folder_path: object, platform_name: str = "tmall"
) -> tuple[Path, ...]:
    """Resolve the source workbook's image folder on the paired computer."""
    folder_path = Path(str(raw_folder_path or "")).expanduser()
    if not folder_path.is_absolute() or not folder_path.is_dir():
        raise RuntimeError("Excel 中的图文图片文件夹不存在或无法读取")
    try:
        image_paths = tuple(
            sorted(
                (
                    path
                    for path in folder_path.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in (
                        JD_ARTICLE_IMAGE_EXTENSIONS
                        if platform_name == "jd"
                        else SUPPORTED_COVER_IMAGE_EXTENSIONS
                    )
                ),
                key=lambda path: (path.name.casefold(), path.name),
            )
        )
    except OSError as exc:
        raise RuntimeError("Excel 中的图文图片文件夹无法读取") from exc
    if platform_name == "tmall":
        max_images = 9
    elif platform_name == "jd":
        max_images = 20
    else:
        max_images = MAX_SOCIAL_ARTICLE_IMAGES
    if not 1 <= len(image_paths) <= max_images:
        raise RuntimeError(
            f"图文图片文件夹必须包含 1-{max_images} 张"
            + (" JPG 或 PNG 图片" if platform_name == "jd" else " JPG、PNG 或 WebP 图片")
        )
    if any(path.stat().st_size == 0 for path in image_paths):
        raise RuntimeError("Excel 中的图文图片不能为空")
    return image_paths


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
        self.claim_wait_seconds = 0.0
        self.lease_seconds = 45.0
        self.stopping = False
        self.authorization_failed = False
        self.runner: AgentJobRunner | None = None
        self.local_upload_server: LocalUploadServer | None = None
        self.hello = {
            "agent_id": self.agent_id,
            "device_name": socket.gethostname() or "Local PC",
            "system": platform.platform()[:200],
            "version": __version__,
            "capabilities": ["local_upload", "self_update"],
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
            if self.local_upload_server is not None:
                self.local_upload_server.stop()
                self.local_upload_server = None
        if self.runner is None:
            self.runner = AgentJobRunner(user["id"], paths)
        if self.local_upload_server is None:
            local_upload_server = LocalUploadServer(
                self.client, self.runner.paths.runtime / "assets"
            )
            try:
                local_upload_server.start()
            except OSError as exc:
                if self.runner is not None:
                    self.runner.shutdown()
                    self.runner = None
                self.local_upload_server = None
                raise RuntimeError(
                    "本机上传服务启动失败，端口 48765 可能已被占用"
                ) from exc
            self.local_upload_server = local_upload_server
        self.poll_seconds = max(1.0, float(response.get("poll_seconds", self.poll_seconds)))
        self.claim_wait_seconds = max(
            0.0, min(30.0, float(response.get("claim_wait_seconds", 0)))
        )
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
                if self.claim_wait_seconds > 0:
                    job = self.client.claim(
                        self.agent_id, wait_seconds=self.claim_wait_seconds
                    )
                else:
                    # Keep compatibility with older embedded/test clients.
                    job = self.client.claim(self.agent_id)
                if job is None:
                    if self.claim_wait_seconds <= 0:
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
        if self.local_upload_server is not None:
            self.local_upload_server.stop()
            self.local_upload_server = None

    def execute(self, job: dict) -> None:
        assert self.runner is not None
        job_id = job["id"]
        label = {"tmall": "天猫", "jd": "京东"}.get(job["platform"], job["platform"])
        print(f"领取任务：{label} / {job['account']} / {job['kind']} / {job_id}")
        video_path: Path | None = None
        cover_image_path: Path | None = None
        image_paths: tuple[Path, ...] = ()
        local_asset_paths: tuple[Path, ...] = ()
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
                payload = job.get("payload", {})
                content_type = payload.get("content_type", "video")
                local_assets = payload.get("local_assets")
                # Older queued tasks omitted the flag and still need the managed download path.
                if isinstance(local_assets, dict):
                    asset_root = self.runner.paths.runtime / "assets"
                    resolved_assets: list[Path] = []
                    if content_type == "article":
                        resolved_images: list[Path] = []
                        for asset in local_assets.get("images") or []:
                            resolved_images.append(
                                _local_asset_path(asset_root, asset, "article-image")
                            )
                            local_asset_paths = tuple(resolved_images)
                        image_paths = tuple(resolved_images)
                        max_images = 20 if job["platform"] == "jd" else MAX_SOCIAL_ARTICLE_IMAGES if job["platform"] in {"xiaohongshu", "douyin"} else 9
                        if not 1 <= len(image_paths) <= max_images:
                            raise RuntimeError("任务中的本机图文素材数量无效")
                        resolved_assets.extend(image_paths)
                    else:
                        video_path = _local_asset_path(
                            asset_root, local_assets.get("video"), "video"
                        )
                        resolved_assets.append(video_path)
                        local_asset_paths = tuple(resolved_assets)
                        if local_assets.get("cover"):
                            cover_image_path = _local_asset_path(
                                asset_root, local_assets["cover"], "cover"
                            )
                            resolved_assets.append(cover_image_path)
                            local_asset_paths = tuple(resolved_assets)
                    local_asset_paths = tuple(resolved_assets)
                elif payload.get("managed_upload", True):
                    download_dir = secure_directory(self.runner.paths.uploads / job_id)
                    if content_type == "article":
                        downloaded_images: list[Path] = []
                        for index, raw_name in enumerate(payload.get("image_filenames") or []):
                            image_name = validate_cover_image_filename(raw_name)
                            image_path = download_dir / image_name
                            print(f"正在下载图文图片：{image_name}")
                            self.client.download_article_image(
                                job_id,
                                self.agent_id,
                                index,
                                image_path,
                                progress=heartbeat_with_grace,
                            )
                            heartbeat_with_grace()
                            if not image_path.is_file() or image_path.stat().st_size == 0:
                                raise RuntimeError("任务图文图片下载为空")
                            downloaded_images.append(image_path)
                        image_paths = tuple(downloaded_images)
                        max_images = 20 if job["platform"] == "jd" else MAX_SOCIAL_ARTICLE_IMAGES if job["platform"] in {"xiaohongshu", "douyin"} else 9
                        if not 1 <= len(image_paths) <= max_images:
                            raise RuntimeError("任务图文图片数量无效")
                    else:
                        original_name = validate_media_filename(
                            payload.get("original_filename") or "video.mp4"
                        )
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
                        raw_cover_name = payload.get("cover_image_filename")
                        if raw_cover_name:
                            cover_name = validate_cover_image_filename(raw_cover_name)
                            cover_image_path = download_dir / cover_name
                            print(f"正在下载自定义封面：{cover_name}")
                            self.client.download_cover_image(
                                job_id,
                                self.agent_id,
                                cover_image_path,
                                progress=heartbeat_with_grace,
                            )
                            heartbeat_with_grace()
                            if (
                                not cover_image_path.is_file()
                                or cover_image_path.stat().st_size == 0
                            ):
                                raise RuntimeError("任务封面图片下载为空")
                else:
                    if content_type == "article":
                        image_folder_path = payload.get("image_folder_path")
                        if image_folder_path:
                            image_paths = _article_images_from_folder(
                                image_folder_path, job["platform"]
                            )
                        else:
                            image_paths = tuple(
                                Path(str(path)).expanduser()
                                for path in payload.get("image_paths") or []
                            )
                            if not image_paths or any(not path.is_file() for path in image_paths):
                                raise RuntimeError("Excel 中的图文图片路径不存在或无法读取")
                    else:
                        video_path = Path(str(payload.get("video_path") or "")).expanduser()
                        if not video_path.is_absolute() or not video_path.is_file():
                            raise RuntimeError("Excel 中的视频本机绝对路径不存在或无法读取")
                        if video_path.stat().st_size == 0:
                            raise RuntimeError("Excel 中的视频文件为空")
                        raw_cover_path = payload.get("cover_image_path")
                        if raw_cover_path:
                            cover_image_path = Path(str(raw_cover_path)).expanduser()
                            if not cover_image_path.is_file() or cover_image_path.stat().st_size == 0:
                                raise RuntimeError("Excel 中的自定义封面不存在、为空或无法读取")

            future = (
                self.runner.submit(job, video_path, cover_image_path, image_paths)
                if image_paths
                else self.runner.submit(job, video_path, cover_image_path)
            )
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
            message = "本地代理任务失败，请查看任务日志"
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
                            logs.append(f"本地临时素材清理失败：{exc}")
                        else:
                            time.sleep(0.5 * (attempt + 1))
            for asset_path in local_asset_paths:
                try:
                    asset_path.unlink(missing_ok=True)
                except OSError as exc:
                    logs.append(f"本机素材清理失败：{exc}")

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
    if os.name != "nt":
        raise SystemExit("MPAU 本地执行助手仅支持 Windows")
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

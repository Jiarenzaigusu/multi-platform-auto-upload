from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from utils.config import BASE_DIR
from webapp.ai_copy import create_ai_copy_router
from webapp.api.agent import create_agent_router
from webapp.api.batch import BatchValidationError
from webapp.api.batch_jd import parse_jd_batch_workbook
from webapp.api.batch_tmall import parse_tmall_batch_workbook
from webapp.api.media import (
    MediaQuotaExceededError,
    UploadTooLargeError,
    directory_usage,
    enforce_media_quota,
    list_media_files,
    resolve_user_media_path,
    stage_upload,
    validate_media_filename,
)
from webapp.api.models import (
    ValidationError,
    validate_account_name,
    validate_platform,
    validate_publish_request,
)
from webapp.api.platforms import delete_account_cookie
from webapp.api.store import TERMINAL_STATUSES
from webapp.api.tasks import TaskManager
from webapp.auth import AuthService, AuthStore, create_auth_router
from webapp.auth.dependencies import require_operator, require_user
from webapp.auth.middleware import AuthenticationMiddleware
from webapp.llm_adapter import create_llm_adapter_router
from webapp.workspaces import AppDataPaths, UserWorkspace, UserWorkspaceRegistry


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Validated process settings shared by HTTP and user-workspace services."""

    data_dir: Path
    frontend_dist_dir: Path
    max_upload_bytes: int = 4 * 1024 * 1024 * 1024
    max_upload_request_bytes: int = 20 * 1024 * 1024 * 1024
    max_media_total_bytes: int = 100 * 1024 * 1024 * 1024
    max_media_files: int = 1000
    max_batch_workbook_bytes: int = 10 * 1024 * 1024
    max_batch_rows: int = 200
    browser_idle_timeout_seconds: float = 5 * 60
    user_workers: int = 1
    global_browser_tasks: int = 10
    agent_installer_path: Path | None = None
    session_seconds: int = 12 * 60 * 60
    secure_cookies: bool = False
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8788",
        "http://127.0.0.1:8788",
    )

    @classmethod
    def from_environment(cls) -> "WebSettings":
        """Load deployment settings while retaining safe local defaults."""
        data_dir = Path(os.getenv("MPAU_DATA_DIR", BASE_DIR / "data"))
        frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"
        raw_idle_timeout = os.getenv("MPAU_BROWSER_IDLE_SECONDS", "300")
        try:
            idle_timeout = max(0.0, float(raw_idle_timeout))
        except ValueError:
            idle_timeout = 5 * 60

        def positive_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        allowed_hosts = tuple(
            value.strip()
            for value in os.getenv(
                "MPAU_ALLOWED_HOSTS", "127.0.0.1,localhost"
            ).split(",")
            if value.strip()
        )
        allowed_origins = tuple(
            value.strip().rstrip("/")
            for value in os.getenv(
                "MPAU_ALLOWED_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173,"
                "http://localhost:8788,http://127.0.0.1:8788",
            ).split(",")
            if value.strip()
        )
        return cls(
            data_dir=data_dir,
            frontend_dist_dir=frontend,
            max_upload_request_bytes=positive_int(
                "MPAU_MAX_UPLOAD_REQUEST_BYTES", 20 * 1024 * 1024 * 1024
            ),
            max_media_total_bytes=positive_int(
                "MPAU_MAX_MEDIA_TOTAL_BYTES", 100 * 1024 * 1024 * 1024
            ),
            max_media_files=positive_int("MPAU_MAX_MEDIA_FILES", 1000),
            browser_idle_timeout_seconds=idle_timeout,
            user_workers=positive_int("MPAU_USER_WORKERS", 1),
            global_browser_tasks=positive_int("MPAU_MAX_BROWSER_TASKS", 10),
            agent_installer_path=Path(
                os.getenv(
                    "MPAU_AGENT_INSTALLER_PATH",
                    str(BASE_DIR / "deploy/windows/output/MPAU-Agent-Setup.exe"),
                )
            ).expanduser().resolve(),
            session_seconds=positive_int("MPAU_SESSION_SECONDS", 12 * 60 * 60),
            secure_cookies=os.getenv("MPAU_SECURE_COOKIES", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )


def _job_response(job: dict) -> dict:
    payload = dict(job)
    payload.pop("payload", None)
    return payload


def _tail_platform_log(
    directory: Path, platform: str, lines: int = 120
) -> list[str]:
    log_path = directory / f"{platform}.log"
    if not log_path.exists():
        return []
    try:
        return log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def _tail_file(path: Path, lines: int = 120) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def create_app(
    settings: WebSettings | None = None,
    workspace_registry: UserWorkspaceRegistry | None = None,
    auth_service: AuthService | None = None,
) -> FastAPI:
    settings = settings or WebSettings.from_environment()
    data_paths = AppDataPaths.create(settings.data_dir)
    batch_template_dir = Path(__file__).resolve().parents[1] / "templates"
    workspace_registry = workspace_registry or UserWorkspaceRegistry(
        data_paths,
        user_workers=settings.user_workers,
        global_browser_tasks=settings.global_browser_tasks,
        browser_idle_timeout_seconds=settings.browser_idle_timeout_seconds,
    )
    auth_service = auth_service or AuthService(
        AuthStore(data_paths.auth_database),
        session_seconds=settings.session_seconds,
    )

    def cleanup_staged_upload(
        directory: Path, manager: TaskManager
    ) -> None:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return
        except OSError as exc:
            manager.record_maintenance_error(f"未入队上传目录清理失败：{directory.name}：{exc}")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            workspace_registry.close()

    app = FastAPI(title="MPAU Commerce Console", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.data_paths = data_paths
    app.state.workspace_registry = workspace_registry
    app.state.auth_service = auth_service
    trusted_browser_origins = set(settings.allowed_origins)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuthenticationMiddleware, service=auth_service)

    @app.middleware("http")
    async def reject_oversized_upload_requests(request: Request, call_next):
        if request.method == "POST" and request.url.path in {
            "/api/media",
            "/api/jobs/publish",
        }:
            raw_length = request.headers.get("content-length")
            try:
                content_length = int(raw_length) if raw_length else None
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Content-Length 无效"})
            if (
                content_length is not None
                and content_length > settings.max_upload_request_bytes
            ):
                return JSONResponse(
                    status_code=413,
                    content={"detail": "上传请求体超过服务器允许的大小"},
                )
        return await call_next(request)

    @app.middleware("http")
    async def reject_cross_site_mutations(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if origin not in trusted_browser_origins:
                return JSONResponse(status_code=403, content={"detail": "拒绝来自未授权页面的写操作"})
        return await call_next(request)

    def current_workspace(request: Request) -> UserWorkspace:
        user = require_user(request)
        return workspace_registry.get(user.id)

    def operator_workspace(request: Request) -> UserWorkspace:
        user = require_operator(request)
        return workspace_registry.get(user.id)

    def delete_account_and_cookie(
        workspace: UserWorkspace, platform: str, account: str
    ) -> dict:
        store = workspace.store
        manager = workspace.task_manager
        deleted_account = store.delete_account(platform, account)
        try:
            manager.close_account_session(platform, account)
            cookie_deleted = delete_account_cookie(workspace.paths, platform, account)
        except RuntimeError as exc:
            store.remember_account(platform, account)
            raise ValueError(str(exc)) from exc
        except OSError:
            store.remember_account(platform, account)
            raise
        return {"account": deleted_account, "cookie_deleted": cookie_deleted}

    def delete_account_after_cancellation(
        user_id: str, job_id: str, platform: str, account: str
    ) -> None:
        """Wait for browser cleanup so a cancelled login cannot recreate its Cookie."""
        workspace = workspace_registry.get(user_id)
        store = workspace.store
        manager = workspace.task_manager
        if not manager.wait_for_account_idle(platform, account, timeout=15 * 60):
            job = store.get_job(job_id)
            if job:
                result = dict(job.get("result") or {})
                result.update(account_deletion="failed", account_deletion_error="等待浏览器退出超时")
                store.update_job(
                    job_id,
                    message="任务中断请求已发送，但等待浏览器退出超时；账号和 Cookie 未删除",
                    result=result,
                )
            return

        try:
            delete_account_and_cookie(workspace, platform, account)
        except (KeyError, ValueError, OSError) as exc:
            job = store.get_job(job_id)
            if job:
                result = dict(job.get("result") or {})
                result.update(account_deletion="failed", account_deletion_error=str(exc))
                store.update_job(
                    job_id,
                    message=f"任务已中断，但删除账号失败：{exc}",
                    result=result,
                )
            return

        job = store.get_job(job_id)
        if job:
            result = dict(job.get("result") or {})
            result.update(account_deletion="completed")
            store.update_job(job_id, result=result)

    frontend_ready = (
        (settings.frontend_dist_dir / "index.html").is_file()
        and (settings.frontend_dist_dir / "assets").is_dir()
    )

    def readiness_status() -> dict:
        maintenance_errors = workspace_registry.maintenance_errors()
        checks = {
            "workspace_registry": workspace_registry.ready,
            "runtime_writable": os.access(data_paths.root, os.W_OK | os.X_OK),
            "frontend_built": frontend_ready,
            "auth_initialized": not auth_service.setup_required(),
            "maintenance_clean": not maintenance_errors,
        }
        return {
            "status": "ready" if all(checks.values()) else "degraded",
            "execution_mode": "local_agent",
            "checks": checks,
            "capacity": {
                "active_jobs_per_agent": 1,
                "browser_capacity_location": "user_device",
            },
            "maintenance_errors": maintenance_errors,
            "platforms": ["tmall", "jd"],
        }

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "execution_mode": "local_agent",
            "platforms": ["tmall", "jd"],
        }

    @app.get("/api/readiness")
    def readiness() -> JSONResponse:
        body = readiness_status()
        return JSONResponse(status_code=200 if body["status"] == "ready" else 503, content=body)

    @app.get("/api/accounts")
    def list_accounts(
        workspace: UserWorkspace = Depends(current_workspace),
    ) -> dict:
        return {"accounts": workspace.store.list_accounts()}

    @app.get("/api/media")
    def list_media(
        workspace: UserWorkspace = Depends(current_workspace),
    ) -> dict:
        """List batch videos owned by the current user."""
        return {"files": list_media_files(workspace.paths.media)}

    @app.post("/api/media", status_code=201)
    async def upload_media(
        files: list[UploadFile] = File(...),
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> JSONResponse:
        """Atomically add videos to the current user's batch media library."""
        if not files or len(files) > 200:
            raise HTTPException(status_code=422, detail="每次请选择 1-200 个视频文件")
        try:
            names = [validate_media_filename(upload.filename or "") for upload in files]
            if len(set(names)) != len(names):
                raise HTTPException(status_code=409, detail="本次上传包含重复文件名")
            destinations = [workspace.paths.media / name for name in names]
            staged_paths: list[Path] = []
            created: list[Path] = []
            try:
                for upload in files:
                    staged = workspace.paths.media / f".{uuid.uuid4().hex}.upload"
                    await asyncio.to_thread(
                        stage_upload, upload, staged, settings.max_upload_bytes
                    )
                    staged_paths.append(staged)

                with workspace.store.media_lock:
                    incoming_bytes = sum(path.stat().st_size for path in staged_paths)
                    enforce_media_quota(
                        workspace.paths.media,
                        incoming_files=len(files),
                        incoming_bytes=incoming_bytes,
                        max_files=settings.max_media_files,
                        max_bytes=settings.max_media_total_bytes,
                    )
                    _, media_bytes = directory_usage(workspace.paths.media)
                    _, upload_bytes = directory_usage(
                        workspace.paths.uploads, recursive=True
                    )
                    if (
                        media_bytes + upload_bytes + incoming_bytes
                        > settings.max_media_total_bytes
                    ):
                        raise MediaQuotaExceededError(
                            "当前用户保存的视频总容量已超过配置上限"
                        )
                    if any(path.exists() for path in destinations):
                        raise HTTPException(status_code=409, detail="素材目录中已存在同名文件")
                    for staged, destination in zip(
                        staged_paths, destinations, strict=True
                    ):
                        staged.replace(destination)
                        created.append(destination)
            except Exception:
                for path in (*staged_paths, *created):
                    path.unlink(missing_ok=True)
                raise
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail="单个视频超过允许的最大文件大小") from exc
        except MediaQuotaExceededError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="素材目录中已存在同名文件") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="素材保存失败，请检查磁盘空间和目录权限") from exc
        finally:
            for upload in files:
                await upload.close()
        return JSONResponse(
            status_code=201,
            content={"files": list_media_files(workspace.paths.media)},
        )

    @app.delete("/api/media/{filename}")
    def delete_media(
        filename: str,
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> dict:
        """Delete an idle batch video while protecting queued and running jobs."""
        try:
            path = resolve_user_media_path(workspace.paths.media, filename)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with workspace.store.media_lock:
            active_jobs = (
                job
                for job in workspace.store.list_jobs(limit=None)
                if job["status"] not in TERMINAL_STATUSES
            )
            if any(
                Path(job.get("payload", {}).get("video_path", "")).resolve() == path
                for job in active_jobs
            ):
                raise HTTPException(status_code=409, detail="该视频正被排队或运行中的任务使用")
            try:
                path.unlink()
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="素材文件不存在") from exc
            except OSError as exc:
                raise HTTPException(status_code=500, detail="删除素材文件失败") from exc
        return {"deleted_name": filename}

    @app.delete("/api/accounts/{platform}/{account}")
    def delete_account(
        platform: str,
        account: str,
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> dict:
        try:
            selected_platform = validate_platform(platform)
            selected_account = validate_account_name(account)
            if getattr(workspace.task_manager, "remote_execution", False):
                if workspace.store.list_active_jobs(selected_platform, selected_account):
                    raise ValueError("该店铺仍有排队或运行中的任务，请先中断任务")
                if not any(
                    item["platform"] == selected_platform
                    and item["account"] == selected_account
                    for item in workspace.store.list_accounts()
                ):
                    raise KeyError(selected_account)
                job = workspace.task_manager.submit_account_task(
                    kind="delete_account",
                    platform=selected_platform,
                    account=selected_account,
                    headed=False,
                )
                return {
                    "account": {
                        "platform": selected_platform,
                        "account": selected_account,
                    },
                    "cookie_deleted": False,
                    "deletion_pending": True,
                    "job": _job_response(job),
                }
            result = delete_account_and_cookie(
                workspace, selected_platform, selected_account
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="店铺账号不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="删除 Cookie 文件失败") from exc
        return result

    @app.get("/api/batch-templates/tmall")
    def download_tmall_batch_template(
        _user=Depends(require_user),
    ) -> FileResponse:
        template_path = batch_template_dir / "tmall_batch_template.xlsx"
        if not template_path.is_file():
            raise HTTPException(status_code=500, detail="天猫批量发布模板尚未生成")
        return FileResponse(
            template_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="tmall_batch_template.xlsx",
        )

    @app.get("/downloads/MPAU-Agent-Setup.exe")
    def download_agent_installer(_user=Depends(require_user)) -> FileResponse:
        installer = settings.agent_installer_path
        if installer is None or not installer.is_file():
            raise HTTPException(
                status_code=404,
                detail="管理员尚未上传 Windows 本地执行助手安装包",
            )
        return FileResponse(
            installer,
            media_type="application/vnd.microsoft.portable-executable",
            filename="MPAU-Agent-Setup.exe",
        )

    @app.get("/api/batch-templates/jd")
    def download_jd_batch_template(
        _user=Depends(require_user),
    ) -> FileResponse:
        template_path = batch_template_dir / "jd_batch_template.xlsx"
        if not template_path.is_file():
            raise HTTPException(status_code=500, detail="京东批量发布模板尚未生成")
        return FileResponse(
            template_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="jd_batch_template.xlsx",
        )

    @app.post("/api/accounts/{platform}/{account}/login", status_code=202)
    def login_account(
        platform: str,
        account: str,
        headed: bool = True,
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> dict:
        try:
            job = workspace.task_manager.submit_account_task(
                kind="login",
                platform=validate_platform(platform),
                account=validate_account_name(account),
                headed=headed,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job": _job_response(job)}

    @app.post("/api/accounts/{platform}/{account}/check", status_code=202)
    def check_account(
        platform: str,
        account: str,
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> dict:
        try:
            job = workspace.task_manager.submit_account_task(
                kind="check",
                platform=validate_platform(platform),
                account=validate_account_name(account),
                headed=False,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job": _job_response(job)}

    @app.get("/api/jobs")
    def list_jobs(
        limit: int = Query(500, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        workspace: UserWorkspace = Depends(current_workspace),
    ) -> dict:
        store = workspace.store
        summary = store.job_summary()
        return {
            "jobs": [_job_response(job) for job in store.list_jobs(limit=limit, offset=offset)],
            "total": summary["total"],
            "status_counts": summary["statuses"],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(
        job_id: str,
        workspace: UserWorkspace = Depends(current_workspace),
    ) -> dict:
        store = workspace.store
        manager = workspace.task_manager
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        log_path = manager.job_log_path(job_id)
        if log_path is not None:
            logs = _tail_file(log_path) if log_path.exists() else []
        else:
            logs = _tail_platform_log(
                workspace.paths.platform_logs, job["platform"]
            )
        return {"job": _job_response(job), "logs": logs}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(
        job_id: str,
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> dict:
        store = workspace.store
        manager = workspace.task_manager
        existing = store.get_job(job_id)
        if not existing:
            raise HTTPException(status_code=404, detail="任务不存在")
        if existing["status"] not in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="仅已完成或失败的任务可以删除")
        try:
            manager.delete_job_artifacts(job_id)
            store.delete_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="删除任务日志失败，任务记录已保留") from exc
        return {"deleted_id": job_id}

    @app.post("/api/jobs/{job_id}/cancel-and-delete-account", status_code=202)
    def cancel_job_and_delete_account(
        job_id: str,
        background_tasks: BackgroundTasks,
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> dict:
        store = workspace.store
        manager = workspace.task_manager
        original_job = store.get_job(job_id)
        if not original_job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if original_job["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="仅排队中或执行中的任务可以中断")
        try:
            affected_jobs = manager.cancel_account_tasks(
                original_job["platform"], original_job["account"]
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        job = store.get_job(job_id) or original_job

        if getattr(manager, "remote_execution", False):
            deletion_job = manager.submit_account_task(
                kind="delete_account",
                platform=job["platform"],
                account=job["account"],
                headed=False,
            )
            return {
                "job": _job_response(job),
                "cancelled_count": len(affected_jobs),
                "account_deletion": "pending",
                "deletion_job": _job_response(deletion_job),
                "message": "已通知本地代理中断任务；浏览器停止后将在用户电脑上删除 Cookie",
            }

        if not store.list_active_jobs(job["platform"], job["account"]):
            try:
                deleted = delete_account_and_cookie(
                    workspace, job["platform"], job["account"]
                )
            except (KeyError, ValueError, OSError) as exc:
                raise HTTPException(status_code=409, detail=f"任务已中断，但删除账号失败：{exc}") from exc
            return {
                "job": _job_response(job),
                "cancelled_count": len(affected_jobs),
                "account_deletion": "completed",
                **deleted,
            }

        background_tasks.add_task(
            delete_account_after_cancellation,
            workspace.user_id,
            job["id"],
            job["platform"],
            job["account"],
        )
        return {
            "job": _job_response(job),
            "cancelled_count": len(affected_jobs),
            "account_deletion": "pending",
            "message": "正在中断该账号的全部活动任务；浏览器退出后会自动删除 Cookie 和账号标识",
        }

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        workspace: UserWorkspace = Depends(current_workspace),
    ) -> StreamingResponse:
        store = workspace.store

        async def event_stream() -> AsyncIterator[str]:
            last_payload = ""
            while True:
                job = store.get_job(job_id)
                if not job:
                    yield "event: error\ndata: {\"detail\": \"任务不存在\"}\n\n"
                    return
                payload = json.dumps(_job_response(job), ensure_ascii=False)
                if payload != last_payload:
                    yield f"event: job\ndata: {payload}\n\n"
                    last_payload = payload
                if job["status"] in TERMINAL_STATUSES:
                    return
                await asyncio.sleep(1)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/jobs/publish", status_code=202)
    async def create_publish_job(
        platform: str = Form(...),
        account: str = Form(...),
        video: UploadFile = File(...),
        title: str = Form(...),
        description: str = Form(""),
        tags: str = Form(""),
        goods_id: str = Form(""),
        activity_topic: str = Form(""),
        music_name: str = Form(""),
        creator_declaration: str = Form("内容无需标注"),
        schedule: str = Form(""),
        original: bool = Form(False),
        dry_run: bool = Form(False),
        headed: bool = Form(True),
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> JSONResponse:
        manager = workspace.task_manager
        try:
            original_name = validate_media_filename(video.filename or "video.mp4")
        except ValueError as exc:
            await video.close()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Start recovery and orphan cleanup before creating a new managed upload directory.
        try:
            manager.start()
        except Exception:
            await video.close()
            raise

        destination_dir = workspace.paths.uploads / uuid.uuid4().hex
        destination_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        destination = destination_dir / original_name
        try:
            await asyncio.to_thread(
                stage_upload,
                video,
                destination,
                settings.max_upload_bytes,
            )
            request = validate_publish_request(
                platform=platform,
                account=account,
                video_path=destination,
                original_filename=original_name,
                title=title,
                description=description,
                raw_tags=tags,
                goods_id=goods_id,
                activity_topic=activity_topic,
                raw_music_name=music_name,
                raw_creator_declaration=creator_declaration,
                raw_schedule=schedule,
                original=original,
                dry_run=dry_run,
                headed=headed,
                managed_upload=True,
            )
        except ValidationError as exc:
            cleanup_staged_upload(destination_dir, manager)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UploadTooLargeError as exc:
            cleanup_staged_upload(destination_dir, manager)
            raise HTTPException(status_code=413, detail="视频超过 Web 应用允许的最大文件大小") from exc
        except HTTPException:
            cleanup_staged_upload(destination_dir, manager)
            raise
        except OSError as exc:
            cleanup_staged_upload(destination_dir, manager)
            raise HTTPException(status_code=500, detail="视频暂存失败，请检查磁盘空间和目录权限") from exc
        except Exception:
            cleanup_staged_upload(destination_dir, manager)
            raise
        finally:
            await video.close()

        def submit_with_quota() -> dict:
            with workspace.store.media_lock:
                _, media_bytes = directory_usage(workspace.paths.media)
                _, upload_bytes = directory_usage(
                    workspace.paths.uploads, recursive=True
                )
                if media_bytes + upload_bytes > settings.max_media_total_bytes:
                    raise MediaQuotaExceededError(
                        "当前用户保存的视频总容量已超过配置上限"
                    )
                return manager.submit_publish_task(request)

        try:
            job = await asyncio.to_thread(submit_with_quota)
        except MediaQuotaExceededError as exc:
            cleanup_staged_upload(destination_dir, manager)
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except Exception:
            cleanup_staged_upload(destination_dir, manager)
            raise
        return JSONResponse(status_code=202, content={"job": _job_response(job)})

    async def create_batch_jobs(
        *,
        platform_label: str,
        parser,
        account: str = Form(...),
        workbook: UploadFile = File(...),
        dry_run: bool = Form(False),
        headed: bool = Form(True),
        workspace: UserWorkspace,
    ) -> JSONResponse:
        original_name = Path(workbook.filename or "").name
        if Path(original_name).suffix.lower() != ".xlsx":
            await workbook.close()
            raise HTTPException(status_code=422, detail=f"请上传 .xlsx 格式的{platform_label}批量发布表格")

        try:
            selected_account = validate_account_name(account)
            content = await workbook.read(settings.max_batch_workbook_bytes + 1)
            if len(content) > settings.max_batch_workbook_bytes:
                raise HTTPException(status_code=413, detail="Excel 文件不能超过 10 MB")
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await workbook.close()

        def parse_and_submit() -> tuple[str, list[dict]]:
            with workspace.store.media_lock:
                rows = parser(
                    content,
                    account=selected_account,
                    dry_run=dry_run,
                    headed=headed,
                    base_dir=workspace.paths.media,
                    max_rows=settings.max_batch_rows,
                )
                batch_id = uuid.uuid4().hex
                jobs = workspace.task_manager.submit_publish_tasks(
                    [(row.request, row.row_number) for row in rows],
                    batch_id=batch_id,
                )
                return batch_id, jobs

        try:
            batch_id, jobs = await asyncio.to_thread(parse_and_submit)
        except BatchValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": str(exc),
                    "errors": [error.to_dict() for error in exc.errors],
                },
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(
            status_code=202,
            content={
                "batch_id": batch_id,
                "created_count": len(jobs),
                "jobs": [_job_response(job) for job in jobs],
            },
        )

    @app.post("/api/jobs/batch/tmall", status_code=202)
    async def create_tmall_batch_jobs(
        account: str = Form(...),
        workbook: UploadFile = File(...),
        dry_run: bool = Form(False),
        headed: bool = Form(True),
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> JSONResponse:
        return await create_batch_jobs(
            platform_label="天猫",
            parser=parse_tmall_batch_workbook,
            account=account,
            workbook=workbook,
            dry_run=dry_run,
            headed=headed,
            workspace=workspace,
        )

    @app.post("/api/jobs/batch/jd", status_code=202)
    async def create_jd_batch_jobs(
        account: str = Form(...),
        workbook: UploadFile = File(...),
        dry_run: bool = Form(False),
        headed: bool = Form(True),
        workspace: UserWorkspace = Depends(operator_workspace),
    ) -> JSONResponse:
        return await create_batch_jobs(
            platform_label="京东",
            parser=parse_jd_batch_workbook,
            account=account,
            workbook=workbook,
            dry_run=dry_run,
            headed=headed,
            workspace=workspace,
        )

    app.include_router(
        create_auth_router(auth_service, secure_cookies=settings.secure_cookies)
    )
    app.include_router(
        create_llm_adapter_router(
            lambda request: current_workspace(request).llm_registry,
            write_authorizer=lambda request: require_operator(request),
        )
    )
    app.include_router(
        create_ai_copy_router(
            lambda request: operator_workspace(request).ai_copy_service
        )
    )
    app.include_router(
        create_agent_router(
            operator_workspace,
            workspace_registry.get,
            auth_service,
            settings.agent_installer_path,
        )
    )

    if frontend_ready:
        app.mount("/assets", StaticFiles(directory=settings.frontend_dist_dir / "assets"), name="assets")

        @app.get("/")
        def frontend_index() -> FileResponse:
            return FileResponse(settings.frontend_dist_dir / "index.html")
    else:

        @app.get("/")
        def frontend_not_built() -> dict:
            return {
                "message": "FastAPI 已启动。请在 webapp/frontend 中执行 corepack pnpm install --frozen-lockfile && corepack pnpm run build，或运行 corepack pnpm run dev。"
            }

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("webapp.api.main:app", host="127.0.0.1", port=8788, reload=False)

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from utils.files import cleanup_old_files
from webapp.api.browser_runtime import BrowserRuntime
from webapp.api.models import PublishRequest
from webapp.api.store import JobStore, utc_now
from webapp.workspaces.paths import UserDataPaths

_UPLOAD_DIRECTORY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_LOGGER = logging.getLogger(__name__)


class RuntimeInstanceLock:
    """Hold one task-manager process per runtime directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file_descriptor: int | None = None

    def acquire(self) -> None:
        if self._file_descriptor is not None:
            return
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.fsync(descriptor)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            os.close(descriptor)
            raise RuntimeError(
                f"运行目录已有任务管理进程，请先关闭其他 MPAU 实例：{self.path}"
            ) from exc
        self._file_descriptor = descriptor

    def release(self) -> None:
        descriptor = self._file_descriptor
        if descriptor is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._file_descriptor = None


class TaskManager:
    """Runs browser work in background threads and serializes each shop account."""

    def __init__(
        self,
        store: JobStore,
        *,
        user_id: str,
        paths: UserDataPaths,
        runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        max_workers: int = 2,
        browser_runtime: BrowserRuntime | None = None,
        browser_slots: threading.BoundedSemaphore | None = None,
        browser_idle_timeout_seconds: float = 20 * 60,
    ):
        self.store = store
        self.user_id = user_id
        self.paths = paths
        self._uses_default_runner = runner is None
        self.browser_runtime = (
            browser_runtime
            if browser_runtime is not None
            else BrowserRuntime(
                user_id=user_id,
                idle_timeout_seconds=browser_idle_timeout_seconds,
                max_sessions=max_workers,
            )
            if self._uses_default_runner
            else None
        )
        self.runner = runner or self._run_platform_task
        self.browser_slots = browser_slots
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mpau-web")
        self.managed_upload_root = paths.uploads
        self.job_log_dir = paths.job_logs
        self._pending_by_account: dict[str, deque[str]] = defaultdict(deque)
        self._active_accounts: set[str] = set()
        self._job_keys: dict[str, str] = {}
        self._futures: dict[str, Future[None]] = {}
        self._running_async_tasks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}
        self._cancel_requested: set[str] = set()
        self._task_guard = threading.RLock()
        self._idle_condition = threading.Condition(self._task_guard)
        self._shutting_down = False
        self._started = False
        self._maintenance_errors: list[str] = []
        self._instance_lock = RuntimeInstanceLock(self.store.data_dir / ".task-manager.lock")

    @property
    def ready(self) -> bool:
        with self._task_guard:
            return self._started and not self._shutting_down

    @property
    def maintenance_errors(self) -> tuple[str, ...]:
        with self._task_guard:
            return tuple(self._maintenance_errors)

    def record_maintenance_error(self, error: str) -> None:
        with self._task_guard:
            if error not in self._maintenance_errors:
                self._maintenance_errors.append(error)

    def start(self) -> None:
        with self._task_guard:
            if self._started:
                return
            if self._shutting_down:
                raise RuntimeError("任务管理器正在关闭，不能重新启动")
            self._instance_lock.acquire()
            try:
                resumable_jobs = self.store.recover_interrupted_jobs()
                jobs = self.store.list_jobs(limit=None)
                for job in jobs:
                    if job.get("status") in {"succeeded", "failed", "cancelled", "uncertain"}:
                        self._cleanup_managed_upload_safely(job)
                for pruned_job in self.store.prune_terminal_jobs():
                    self.delete_job_artifacts(pruned_job["id"])
                self._cleanup_orphaned_uploads(self.store.list_jobs(limit=None))
                cleanup_old_files(
                    self.paths.media,
                    older_than_days=1,
                    suffixes={".upload"},
                )
                self._started = True
                for job_id in resumable_jobs:
                    self._submit(job_id)
            except Exception:
                self._started = False
                self._instance_lock.release()
                raise

    def submit_account_task(
        self, *, kind: str, platform: str, account: str, headed: bool = True
    ) -> dict[str, Any]:
        self.start()
        job = self.store.create_job(
            kind=kind,
            platform=platform,
            account=account,
            payload={"headed": headed},
        )
        try:
            with self._task_guard:
                self._enqueue_jobs_locked([job])
        except Exception as exc:
            self.store.update_job(
                job["id"],
                status="failed",
                message="任务调度失败",
                error=str(exc),
                finished_at=utc_now(),
            )
            raise
        return job

    def submit_publish_task(
        self,
        request: PublishRequest,
        *,
        batch_id: str | None = None,
        source_row: int | None = None,
    ) -> dict[str, Any]:
        return self.submit_publish_tasks(
            [(request, source_row)], batch_id=batch_id
        )[0]

    def submit_publish_tasks(
        self,
        requests: list[tuple[PublishRequest, int | None]],
        *,
        batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not requests:
            return []
        self.start()
        definitions: list[dict[str, Any]] = []
        for request, source_row in requests:
            payload = asdict(request)
            payload["video_path"] = str(request.video_path)
            payload["cover_image_path"] = (
                str(request.cover_image_path) if request.cover_image_path else None
            )
            payload["schedule"] = request.schedule.isoformat() if request.schedule else None
            payload["tags"] = list(request.tags)
            definitions.append(
                {
                    "kind": "publish",
                    "platform": request.platform,
                    "account": request.account,
                    "payload": payload,
                    "batch_id": batch_id,
                    "source_row": source_row,
                }
            )

        with self._task_guard:
            if self._shutting_down:
                raise RuntimeError("任务管理器正在关闭，不能提交新任务")
            jobs = self.store.create_jobs(definitions)
            try:
                self._enqueue_jobs_locked(jobs)
            except Exception as exc:
                for job in jobs:
                    self.store.update_job(
                        job["id"],
                        status="failed",
                        message="任务调度失败",
                        error=str(exc),
                        finished_at=utc_now(),
                    )
                    self._cleanup_managed_upload_safely(job)
                raise
        return jobs

    def shutdown(self) -> None:
        immediately_cancelled: list[str] = []
        with self._task_guard:
            if self._shutting_down:
                return
            self._shutting_down = True

            for queue in self._pending_by_account.values():
                immediately_cancelled.extend(queue)
                queue.clear()

            for job_id, future in list(self._futures.items()):
                self._cancel_requested.add(job_id)
                if future.cancel():
                    immediately_cancelled.append(job_id)
                    key = self._job_keys.pop(job_id, None)
                    if key:
                        self._active_accounts.discard(key)
                    self._futures.pop(job_id, None)
                    continue
                running_task = self._running_async_tasks.get(job_id)
                if running_task:
                    loop, task = running_task
                    loop.call_soon_threadsafe(task.cancel)

            for job_id in immediately_cancelled:
                job = self.store.get_job(job_id)
                if job and job["status"] not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "uncertain",
                }:
                    self.store.update_job(
                        job_id,
                        status="cancelled",
                        message="服务关闭，任务在启动浏览器前已中断",
                        finished_at=utc_now(),
                    )
                    self._cleanup_managed_upload_safely(job)
                self._job_keys.pop(job_id, None)
                self._cancel_requested.discard(job_id)
            self._idle_condition.notify_all()

        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            try:
                if self.browser_runtime:
                    self.browser_runtime.shutdown()
            finally:
                self._instance_lock.release()

    def cancel_task(self, job_id: str) -> dict[str, Any]:
        """Cancel queued work immediately or signal cancellation to its async browser task."""
        self.start()
        with self._task_guard:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(f"任务不存在：{job_id}")
            if job["status"] in {"succeeded", "failed", "cancelled", "uncertain"}:
                raise ValueError("仅排队中或执行中的任务可以中断")
            if job["status"] == "cancelling":
                return job

            key = self._job_keys.get(job_id) or self._account_key(job["platform"], job["account"])
            queue = self._pending_by_account.get(key)
            if queue and job_id in queue:
                queue.remove(job_id)
                self._job_keys.pop(job_id, None)
                cancelled = self.store.update_job(
                    job_id,
                    status="cancelled",
                    message="任务已中断，未启动浏览器自动化",
                    finished_at=utc_now(),
                )
                self._cleanup_managed_upload_safely(job)
                self._idle_condition.notify_all()
                return cancelled

            future = self._futures.get(job_id)
            if future is None and job["status"] == "running":
                raise RuntimeError("该任务不是当前服务启动，无法确认浏览器已停止；请先停止旧服务")

            self._cancel_requested.add(job_id)
            if future and future.cancel():
                self._futures.pop(job_id, None)
                self._active_accounts.discard(key)
                self._job_keys.pop(job_id, None)
                cancelled = self.store.update_job(
                    job_id,
                    status="cancelled",
                    message="任务已中断，未启动浏览器自动化",
                    finished_at=utc_now(),
                )
                self._cleanup_managed_upload_safely(job)
                self._start_next_locked(key)
                self._idle_condition.notify_all()
                return cancelled

            running_task = self._running_async_tasks.get(job_id)
            if running_task:
                loop, task = running_task
                loop.call_soon_threadsafe(task.cancel)

        return self.store.update_job(
            job_id,
            status="cancelling",
            message="正在中断浏览器任务，完成后将删除该店铺账号与 Cookie",
        )

    def cancel_account_tasks(self, platform: str, account: str) -> list[dict[str, Any]]:
        cancelled: list[dict[str, Any]] = []
        jobs = sorted(
            self.store.list_active_jobs(platform, account),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
        for job in jobs:
            try:
                cancelled.append(self.cancel_task(job["id"]))
            except ValueError:
                continue
        return cancelled

    def wait_for_account_idle(self, platform: str, account: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._idle_condition:
            while self.store.list_active_jobs(platform, account):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle_condition.wait(timeout=min(remaining, 1.0))
        return True

    def job_log_path(self, job_id: str) -> Path | None:
        if not self.job_log_dir or not job_id.isalnum():
            return None
        return self.job_log_dir / f"{job_id}.log"

    def delete_job_artifacts(self, job_id: str) -> None:
        log_path = self.job_log_path(job_id)
        if log_path:
            log_path.unlink(missing_ok=True)

    def delete_jobs_artifacts(self, job_ids: list[str]) -> None:
        """Delete per-job logs for a batch task deletion request."""
        first_error: OSError | None = None
        for job_id in job_ids:
            try:
                self.delete_job_artifacts(job_id)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def close_account_session(self, platform: str, account: str) -> None:
        if not self.browser_runtime or platform not in {"tmall", "jd"}:
            return
        from webapp.api.platforms import resolve_account_file

        account_file = resolve_account_file(self.paths, platform, account)
        self.browser_runtime.close_account(platform, str(account_file))

    def _enqueue_jobs_locked(self, jobs: list[dict[str, Any]]) -> None:
        if self._shutting_down:
            raise RuntimeError("任务管理器正在关闭，不能提交新任务")
        keys: set[str] = set()
        for job in jobs:
            job_id = job["id"]
            key = self._account_key(job["platform"], job["account"])
            self._job_keys[job_id] = key
            self._pending_by_account[key].append(job_id)
            keys.add(key)
        for key in keys:
            self._start_next_locked(key)

    def _submit(self, job_id: str) -> None:
        if not self._started:
            self.start()
        job = self.store.get_job(job_id)
        if not job:
            return
        with self._task_guard:
            self._enqueue_jobs_locked([job])

    @staticmethod
    def _account_key(platform: str, account: str) -> str:
        return f"{platform}:{account}"

    def _start_next_locked(self, key: str) -> None:
        if self._shutting_down or key in self._active_accounts:
            return
        queue = self._pending_by_account.get(key)
        while queue:
            job_id = queue.popleft()
            job = self.store.get_job(job_id)
            if not job or job["status"] in {"succeeded", "failed", "cancelled", "uncertain"}:
                self._job_keys.pop(job_id, None)
                continue
            self._active_accounts.add(key)
            try:
                future = self.executor.submit(self._execute, job_id, key)
            except RuntimeError as exc:
                self._active_accounts.discard(key)
                self._job_keys.pop(job_id, None)
                self.store.update_job(
                    job_id,
                    status="failed",
                    message="任务调度失败",
                    error=str(exc),
                    finished_at=utc_now(),
                )
                continue
            self._futures[job_id] = future
            return
        self._pending_by_account.pop(key, None)

    def _execute(self, job_id: str, key: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            with self._task_guard:
                self._futures.pop(job_id, None)
                self._job_keys.pop(job_id, None)
                self._active_accounts.discard(key)
                self._start_next_locked(key)
                self._idle_condition.notify_all()
            return
        log_sink_id: int | None = None
        try:
            with self._task_guard:
                cancelled_before_start = job_id in self._cancel_requested
            if cancelled_before_start:
                self.store.update_job(
                    job_id,
                    status="cancelled",
                    message="任务已中断，未启动浏览器自动化",
                    finished_at=utc_now(),
                )
                return

            self.store.update_job(
                job_id,
                status="running",
                message="浏览器任务正在运行，请留意本机 Microsoft Edge 与平台安全验证",
                started_at=utc_now(),
            )
            try:
                log_sink_id = self._attach_job_log(job)
                from loguru import logger

                with logger.contextualize(job_id=job_id, user_id=self.user_id):
                    slot = self.browser_slots or nullcontext()
                    with slot:
                        result = self.runner(job)
            except asyncio.CancelledError:
                self.store.update_job(
                    job_id,
                    status="cancelled",
                    message="浏览器任务已中断",
                    finished_at=utc_now(),
                )
                return
            except Exception as exc:
                from uploader.errors import PublishResultUncertainError

                if isinstance(exc, PublishResultUncertainError):
                    self.store.update_job(
                        job_id,
                        status="uncertain",
                        message="平台提交结果无法确认，请先到平台后台核对，确认前不要重试",
                        error=str(exc),
                        finished_at=utc_now(),
                    )
                    return
                self.store.update_job(
                    job_id,
                    status="failed",
                    message="任务失败，请查看任务日志",
                    error=str(exc),
                    finished_at=utc_now(),
                )
                return
            # A runner that returned normally completed before cancellation took effect.
            # Preserve that confirmed result instead of reporting a misleading cancellation.
            self.store.update_job(
                job_id,
                status="succeeded",
                message=result.get("message", "任务已完成"),
                result=result,
                finished_at=utc_now(),
            )
        finally:
            if log_sink_id is not None:
                from loguru import logger

                try:
                    logger.remove(log_sink_id)
                except ValueError:
                    pass
            self._cleanup_managed_upload_safely(job)
            with self._task_guard:
                self._futures.pop(job_id, None)
                self._running_async_tasks.pop(job_id, None)
                self._cancel_requested.discard(job_id)
                self._job_keys.pop(job_id, None)
                self._active_accounts.discard(key)
                self._start_next_locked(key)
                self._idle_condition.notify_all()

    def _attach_job_log(self, job: dict[str, Any]) -> int | None:
        log_path = self.job_log_path(job["id"])
        if not log_path:
            return None
        from loguru import logger

        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        log_path.chmod(0o600)
        platform = job["platform"]
        job_id = job["id"]
        return logger.add(
            log_path,
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level}: {message}",
            opener=lambda path, flags: os.open(path, flags, 0o600),
            filter=lambda record: (
                record["extra"].get("job_id") == job_id
                and record["extra"].get("business_name") == platform
            ),
        )

    def _cleanup_managed_upload(self, job: dict[str, Any]) -> None:
        payload = job.get("payload") or {}
        if not self.managed_upload_root or not payload.get("managed_upload"):
            return
        video_path = Path(payload["video_path"]).resolve()
        parent = video_path.parent
        if (
            parent.parent != self.managed_upload_root
            or not _UPLOAD_DIRECTORY_PATTERN.fullmatch(parent.name)
            or not video_path.is_relative_to(self.managed_upload_root)
        ):
            raise ValueError("任务中的临时视频路径不属于受管上传目录")
        if parent.exists():
            shutil.rmtree(parent)

    def _cleanup_managed_upload_safely(self, job: dict[str, Any]) -> None:
        try:
            self._cleanup_managed_upload(job)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            error = f"临时视频清理失败：{exc}"
            self.record_maintenance_error(error)
            try:
                current = self.store.get_job(job.get("id", ""))
            except (OSError, RuntimeError):
                _LOGGER.exception("无法读取任务状态以记录临时视频清理错误")
                return
            if current:
                result = dict(current.get("result") or {})
                result["cleanup_error"] = str(exc)
                message = current.get("message", "任务已结束")
                if "临时视频清理失败" not in message:
                    message = f"{message}；临时视频清理失败，请检查磁盘"
                try:
                    self.store.update_job(job["id"], result=result, message=message)
                except (KeyError, OSError, RuntimeError):
                    _LOGGER.exception("无法记录任务临时视频清理错误")

    def _cleanup_orphaned_uploads(self, jobs: list[dict[str, Any]]) -> None:
        if not self.managed_upload_root:
            return
        self.managed_upload_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.managed_upload_root.chmod(0o700)
        referenced: set[Path] = set()
        for job in jobs:
            payload = job.get("payload") or {}
            if not payload.get("managed_upload") or not payload.get("video_path"):
                continue
            try:
                referenced.add(Path(payload["video_path"]).resolve().parent)
            except (OSError, RuntimeError, ValueError):
                continue

        for child in self.managed_upload_root.iterdir():
            if (
                not child.is_dir()
                or not _UPLOAD_DIRECTORY_PATTERN.fullmatch(child.name)
                or child.resolve() in referenced
            ):
                continue
            try:
                shutil.rmtree(child)
            except OSError as exc:
                error = f"孤儿上传目录清理失败：{child.name}：{exc}"
                self.record_maintenance_error(error)

    def _run_platform_task(self, job: dict[str, Any]) -> dict[str, Any]:
        if self.browser_runtime is None:
            raise RuntimeError("平台浏览器任务缺少 BrowserRuntime")
        return self.browser_runtime.run(self._run_cancellable_platform_task(job))

    async def _run_cancellable_platform_task(self, job: dict[str, Any]) -> dict[str, Any]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("无法注册浏览器任务的取消控制")

        job_id = job["id"]
        loop = asyncio.get_running_loop()
        with self._task_guard:
            self._running_async_tasks[job_id] = (loop, task)
            cancel_requested = job_id in self._cancel_requested
        if cancel_requested:
            task.cancel()

        try:
            from loguru import logger

            with logger.contextualize(job_id=job_id, user_id=self.user_id):
                return await self._run_platform_task_async(job)
        finally:
            with self._task_guard:
                self._running_async_tasks.pop(job_id, None)

    async def _run_platform_task_async(self, job: dict[str, Any]) -> dict[str, Any]:
        # Import lazily so API startup does not initialize browser automation.
        from webapp.api.platforms import (
            JdVideoUploadRequest,
            TmallVideoUploadRequest,
            check_jd_account,
            check_tmall_account,
            login_jd_account,
            login_tmall_account,
            tmall_publish_strategy,
            upload_jd_video,
            upload_tmall_video,
        )

        platform = job["platform"]
        account = job["account"]
        payload = job["payload"]
        headed = bool(payload.get("headed", True))
        session_pool = None
        if self.browser_runtime is not None and self.browser_runtime.is_current_loop():
            session_pool = (
                self.browser_runtime.tmall_sessions()
                if platform == "tmall"
                else self.browser_runtime.jd_sessions()
            )

        if job["kind"] == "login":
            if platform == "tmall":
                if session_pool is None:
                    raise RuntimeError("天猫任务必须通过 BrowserRuntime 会话池运行")
                result = await login_tmall_account(
                    account,
                    headless=not headed,
                    paths=self.paths,
                    session_pool=session_pool,
                )
            else:
                if session_pool is None:
                    raise RuntimeError("京东任务必须通过 BrowserRuntime 会话池运行")
                result = await login_jd_account(
                    account,
                    headless=not headed,
                    paths=self.paths,
                    session_pool=session_pool,
                )
            if not result.get("success"):
                raise RuntimeError(result.get("message", "登录失败"))
            return {"message": result.get("message", "登录完成")}

        if job["kind"] == "check":
            if platform == "tmall":
                if session_pool is None:
                    raise RuntimeError("天猫任务必须通过 BrowserRuntime 会话池运行")
                valid = await check_tmall_account(
                    account, paths=self.paths, session_pool=session_pool
                )
            else:
                if session_pool is None:
                    raise RuntimeError("京东任务必须通过 BrowserRuntime 会话池运行")
                valid = await check_jd_account(
                    account, paths=self.paths, session_pool=session_pool
                )
            if not valid:
                raise RuntimeError("Cookie 不存在或已失效，请先执行登录")
            return {"message": "账号 Cookie 有效"}

        if job["kind"] != "publish":
            raise RuntimeError(f"未知任务类型：{job['kind']}")

        schedule = datetime.fromisoformat(payload["schedule"]) if payload.get("schedule") else None
        if schedule:
            from webapp.api.models import MIN_SCHEDULE_LEAD_TIME

            now = datetime.now()
            if schedule <= now:
                raise RuntimeError("定时发布时间已过，请重新创建任务")
            if schedule <= now + MIN_SCHEDULE_LEAD_TIME:
                raise RuntimeError("定时发布时间距离当前不足 2 小时，请重新创建任务")
        video_path = Path(payload["video_path"])
        if not video_path.is_file():
            raise RuntimeError("视频文件在任务执行前已被移动或删除")
        if platform == "tmall":
            if session_pool is None:
                raise RuntimeError("天猫任务必须通过 BrowserRuntime 会话池运行")
            request = TmallVideoUploadRequest(
                account_name=account,
                video_file=video_path,
                cover_image_file=(
                    Path(payload["cover_image_path"])
                    if payload.get("cover_image_path")
                    else None
                ),
                title=payload["title"],
                description=payload["description"],
                tags=payload["tags"],
                goods_id=payload["goods_id"],
                activity_topic=payload["activity_topic"],
                music_name=payload.get("music_name", ""),
                creator_declaration=payload.get("creator_declaration", "内容无需标注"),
                schedule=schedule,
                publish_strategy=tmall_publish_strategy(schedule),
                debug=True,
                headless=not headed,
                dry_run=bool(payload["dry_run"]),
            )
            platform_result = await upload_tmall_video(
                request, paths=self.paths, session_pool=session_pool
            )
        else:
            if session_pool is None:
                raise RuntimeError("京东任务必须通过 BrowserRuntime 会话池运行")
            request = JdVideoUploadRequest(
                account_name=account,
                video_file=video_path,
                title=payload["title"],
                goods_id=payload["goods_id"],
                schedule=schedule,
                original=bool(payload["original"]),
                creator_declaration=payload.get("creator_declaration", "内容无需标注"),
                debug=True,
                headless=not headed,
                dry_run=bool(payload["dry_run"]),
            )
            platform_result = await upload_jd_video(
                request, paths=self.paths, session_pool=session_pool
            )

        action = "流程验证已完成，未提交发布" if payload["dry_run"] else "平台已确认接收发布"
        confirmation = platform_result if isinstance(platform_result, dict) else {}
        return {"message": action, "platform_confirmation": confirmation}

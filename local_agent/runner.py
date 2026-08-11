from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from utils.files import cleanup_old_directories, cleanup_old_files
from utils.log import UserLogSinks, create_user_log_sinks
from webapp.ai_copy.product_lookup.tmall_client import (
    BrowserRuntimeTmallPageFetcher,
    DirectoryTmallStorageStateProvider,
)
from webapp.ai_copy.product_lookup.tmall_reader import TmallProductReader
from webapp.api.browser_runtime import BrowserRuntime
from webapp.api.models import MIN_SCHEDULE_LEAD_TIME
from webapp.api.platforms import (
    JdVideoUploadRequest,
    TmallVideoUploadRequest,
    check_jd_account,
    check_tmall_account,
    login_jd_account,
    login_tmall_account,
    resolve_account_file,
    tmall_publish_strategy,
    upload_jd_video,
    upload_tmall_video,
)
from webapp.workspaces.paths import UserDataPaths


class AgentJobRunner:
    """Execute one claimed cloud task in the local user's Edge installation."""

    def __init__(
        self,
        user_id: str,
        paths: UserDataPaths,
        *,
        browser_idle_timeout_seconds: float = 5 * 60,
    ) -> None:
        self.user_id = user_id
        self.paths = paths
        self.runtime = BrowserRuntime(
            user_id=user_id,
            idle_timeout_seconds=browser_idle_timeout_seconds,
            max_sessions=2,
        )
        self.log_sinks: UserLogSinks = create_user_log_sinks(
            user_id, paths.platform_logs
        )
        cleanup_old_files(paths.job_logs, older_than_days=30, suffixes={".log"})
        cleanup_old_files(
            paths.screenshots,
            older_than_days=30,
            suffixes={".png", ".jpg", ".jpeg"},
        )
        cleanup_old_directories(paths.uploads, older_than_days=1)
        self._job_sink_ids: dict[str, int] = {}
        self._task_guard = threading.RLock()
        self._running_tasks: dict[
            str, tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]
        ] = {}
        self._cancel_requested: set[str] = set()

    def submit(
        self, job: dict[str, Any], video_path: Path | None
    ) -> Future[dict[str, Any]]:
        self._attach_job_log(job)
        return self.runtime.submit(self._run_contextualized(job, video_path))

    async def _run_contextualized(
        self, job: dict[str, Any], video_path: Path | None
    ) -> dict[str, Any]:
        job_id = job["id"]
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("无法注册本地浏览器任务的取消控制")
        loop = asyncio.get_running_loop()
        with self._task_guard:
            self._running_tasks[job_id] = (loop, task)
            cancel_requested = job_id in self._cancel_requested
        if cancel_requested:
            task.cancel()
        try:
            with logger.contextualize(job_id=job_id, user_id=self.user_id):
                return await self._run_job(job, video_path)
        finally:
            with self._task_guard:
                self._running_tasks.pop(job_id, None)
                self._cancel_requested.discard(job_id)

    def cancel(self, job_id: str) -> None:
        """Cancel the asyncio task without cancelling its result Future.

        Keeping the outer Future alive lets an uploader turn cancellation after
        the publish click into PublishResultUncertainError.
        """
        with self._task_guard:
            self._cancel_requested.add(job_id)
            running = self._running_tasks.get(job_id)
        if running is not None:
            loop, task = running
            loop.call_soon_threadsafe(task.cancel)

    async def _run_job(
        self, job: dict[str, Any], video_path: Path | None
    ) -> dict[str, Any]:
        platform = job["platform"]
        account = job["account"]
        payload = job.get("payload") or {}
        headed = bool(payload.get("headed", True))
        session_pool = (
            self.runtime.tmall_sessions()
            if platform == "tmall"
            else self.runtime.jd_sessions()
        )

        if job["kind"] == "inspect_product":
            if platform != "tmall":
                raise RuntimeError("本地商品读取目前仅支持天猫")
            product_url = str(payload.get("product_url") or "")
            fetcher = BrowserRuntimeTmallPageFetcher(
                self.runtime,
                DirectoryTmallStorageStateProvider(
                    self.paths.cookies / "tmall", max_candidates=2
                ),
                timeout_seconds=20,
                max_bytes=1_500_000,
            )
            fetched_page = await fetcher.get_async(product_url)

            class _FetchedPageReader:
                def get(self, _product_url: str):
                    return fetched_page

            reference = TmallProductReader(_FetchedPageReader()).inspect(product_url)
            return {
                "message": "已通过用户电脑上的天猫登录状态读取商品",
                "reference": reference.model_dump(mode="json"),
            }

        if job["kind"] == "login":
            if platform == "tmall":
                result = await login_tmall_account(
                    account,
                    headless=not headed,
                    paths=self.paths,
                    session_pool=session_pool,
                )
            else:
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
                valid = await check_tmall_account(
                    account, paths=self.paths, session_pool=session_pool
                )
            else:
                valid = await check_jd_account(
                    account, paths=self.paths, session_pool=session_pool
                )
            if not valid:
                raise RuntimeError("Cookie 不存在或已失效，请先执行登录")
            return {"message": "用户电脑上的账号 Cookie 有效"}

        if job["kind"] == "delete_account":
            account_file = resolve_account_file(self.paths, platform, account)
            await session_pool.close_account(str(account_file))
            try:
                account_file.unlink()
                deleted = True
            except FileNotFoundError:
                deleted = False
            return {
                "message": "用户电脑上的店铺 Cookie 已删除",
                "cookie_deleted": deleted,
            }

        if job["kind"] != "publish":
            raise RuntimeError(f"未知本地代理任务类型：{job['kind']}")
        if video_path is None or not video_path.is_file():
            raise RuntimeError("任务视频没有下载到用户电脑")

        schedule = (
            datetime.fromisoformat(payload["schedule"])
            if payload.get("schedule")
            else None
        )
        if schedule:
            now = datetime.now(tz=schedule.tzinfo) if schedule.tzinfo else datetime.now()
            if schedule <= now + MIN_SCHEDULE_LEAD_TIME:
                raise RuntimeError("定时发布时间距离当前不足 2 小时，请重新创建任务")

        if platform == "tmall":
            request = TmallVideoUploadRequest(
                account_name=account,
                video_file=video_path,
                title=payload["title"],
                description=payload["description"],
                tags=list(payload["tags"]),
                goods_id=payload["goods_id"],
                activity_topic=payload["activity_topic"],
                music_name=payload.get("music_name", ""),
                creator_declaration=payload.get(
                    "creator_declaration", "内容无需标注"
                ),
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
            request = JdVideoUploadRequest(
                account_name=account,
                video_file=video_path,
                title=payload["title"],
                goods_id=payload["goods_id"],
                schedule=schedule,
                original=bool(payload["original"]),
                creator_declaration=payload.get(
                    "creator_declaration", "内容无需标注"
                ),
                debug=True,
                headless=not headed,
                dry_run=bool(payload["dry_run"]),
            )
            platform_result = await upload_jd_video(
                request, paths=self.paths, session_pool=session_pool
            )

        action = (
            "流程验证已在用户电脑完成，未提交发布"
            if payload["dry_run"]
            else "平台已确认接收发布"
        )
        return {
            "message": action,
            "platform_confirmation": (
                platform_result if isinstance(platform_result, dict) else {}
            ),
        }

    def _attach_job_log(self, job: dict[str, Any]) -> None:
        path = self.paths.job_logs / f"{job['id']}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        job_id = job["id"]
        platform = job["platform"]
        self._job_sink_ids[job_id] = logger.add(
            path,
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level}: {message}",
            opener=lambda target, flags: os.open(target, flags, 0o600),
            filter=lambda record: (
                record["extra"].get("job_id") == job_id
                and record["extra"].get("business_name") == platform
            ),
        )

    def finish_logs(self, job_id: str) -> list[str]:
        sink_id = self._job_sink_ids.pop(job_id, None)
        if sink_id is not None:
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        path = self.paths.job_logs / f"{job_id}.log"
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        except OSError:
            return []

    def shutdown(self) -> None:
        with self._task_guard:
            job_ids = list(self._running_tasks)
        for job_id in job_ids:
            self.cancel(job_id)
        for job_id in list(self._job_sink_ids):
            self.finish_logs(job_id)
        try:
            self.runtime.shutdown()
        finally:
            self.log_sinks.close()

from __future__ import annotations

import shutil
import threading
from typing import Callable

from utils.log import create_user_log_sinks
from webapp.ai_copy.product_lookup.agent_reader import AgentTmallProductReader
from webapp.ai_copy.product_lookup.tmall_client import (
    BrowserRuntimeTmallPageFetcher,
    DirectoryTmallStorageStateProvider,
)
from webapp.ai_copy.settings import AiCopySettings
from webapp.api.agent_tasks import AgentTaskManager
from webapp.api.store import JobStore
from webapp.llm_adapter import FileAdapterCredentialStore, LLMAdapterRegistry
from webapp.server_adapters.ai_copy import build_server_ai_copy_service
from webapp.workspaces.paths import AppDataPaths
from webapp.workspaces.service import UserWorkspace


class UserWorkspaceRegistry:
    """Lazily create and own exactly one isolated workspace per user."""

    def __init__(
        self,
        data_paths: AppDataPaths,
        *,
        user_workers: int,
        global_browser_tasks: int,
        browser_idle_timeout_seconds: float,
        manager_factory: Callable[..., object] = AgentTaskManager,
    ) -> None:
        self.data_paths = data_paths
        self.user_workers = max(1, user_workers)
        self.global_browser_tasks = max(1, global_browser_tasks)
        self.browser_idle_timeout_seconds = max(0.0, browser_idle_timeout_seconds)
        self._browser_slots = threading.BoundedSemaphore(
            self.global_browser_tasks
        )
        self._manager_factory = manager_factory
        self._workspaces: dict[str, UserWorkspace] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def ready(self) -> bool:
        with self._lock:
            return not self._closed

    def get(self, user_id: str) -> UserWorkspace:
        with self._lock:
            if self._closed:
                raise RuntimeError("用户工作区注册表已经关闭")
            existing = self._workspaces.get(user_id)
            if existing is not None:
                return existing
            workspace = self._build(user_id)
            self._workspaces[user_id] = workspace
            return workspace

    def _build(self, user_id: str) -> UserWorkspace:
        """Assemble all services owned by one user and unwind partial failures."""
        paths = self.data_paths.for_user(user_id)
        store = JobStore(paths.runtime)
        log_sinks = create_user_log_sinks(user_id, paths.platform_logs)
        manager: object | None = None
        try:
            manager = self._manager_factory(
                store,
                user_id=user_id,
                paths=paths,
                max_workers=self.user_workers,
                browser_slots=self._browser_slots,
                browser_idle_timeout_seconds=self.browser_idle_timeout_seconds,
            )
            llm_registry = LLMAdapterRegistry(
                FileAdapterCredentialStore(paths.secrets / "llm-adapter-credentials.json")
            )
            ai_settings = AiCopySettings()
            tmall_fetcher = None
            tmall_product_reader = None
            if manager.browser_runtime is not None:
                tmall_fetcher = BrowserRuntimeTmallPageFetcher(
                    manager.browser_runtime,
                    DirectoryTmallStorageStateProvider(
                        paths.cookies / "tmall",
                        max_candidates=ai_settings.tmall_account_attempts,
                    ),
                    timeout_seconds=ai_settings.product_timeout_seconds,
                    max_bytes=ai_settings.max_product_page_bytes,
                    browser_slots=self._browser_slots,
                )
            elif getattr(manager, "remote_execution", False):
                tmall_product_reader = AgentTmallProductReader(
                    manager,
                    timeout_seconds=max(60.0, ai_settings.product_timeout_seconds + 10),
                )
            ai_copy_service = build_server_ai_copy_service(
                llm_registry,
                ai_settings,
                tmall_page_fetcher=tmall_fetcher,
                agent_tmall_reader=tmall_product_reader,
            )
        except Exception:
            try:
                if manager is not None:
                    manager.shutdown()
            finally:
                log_sinks.close()
            raise
        return UserWorkspace(
            user_id=user_id,
            paths=paths,
            store=store,
            task_manager=manager,
            llm_registry=llm_registry,
            ai_copy_service=ai_copy_service,
            log_sinks=log_sinks,
        )

    def maintenance_errors(self) -> list[str]:
        with self._lock:
            workspaces = list(self._workspaces.values())
        return [
            f"{workspace.user_id}: {error}"
            for workspace in workspaces
            for error in workspace.task_manager.maintenance_errors
        ]

    def close_user(self, user_id: str) -> None:
        with self._lock:
            workspace = self._workspaces.pop(user_id, None)
        if workspace is not None:
            workspace.close()

    def delete_user_data(self, user_id: str) -> None:
        """Close and remove all persisted workspace files owned by one user."""
        self.close_user(user_id)
        paths = self.data_paths.for_user(user_id)
        shutil.rmtree(paths.root, ignore_errors=False)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workspaces = list(self._workspaces.values())
            self._workspaces.clear()
        for workspace in workspaces:
            workspace.close()

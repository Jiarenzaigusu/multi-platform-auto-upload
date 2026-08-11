from __future__ import annotations

from dataclasses import dataclass, field
import threading

from utils.log import UserLogSinks
from webapp.ai_copy.service import AiCopyService
from webapp.api.store import JobStore
from webapp.llm_adapter.registry import LLMAdapterRegistry
from webapp.workspaces.paths import UserDataPaths


@dataclass(slots=True)
class UserWorkspace:
    """Cohesive owner of one user's stateful publishing services."""

    user_id: str
    paths: UserDataPaths
    store: JobStore
    task_manager: object
    llm_registry: LLMAdapterRegistry
    ai_copy_service: AiCopyService
    log_sinks: UserLogSinks
    _closed: bool = False
    _close_lock: threading.Lock = field(default_factory=threading.Lock)

    def close(self) -> None:
        """Stop browser work before releasing logging resources."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.task_manager.shutdown()
        finally:
            self.log_sinks.close()

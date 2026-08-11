from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
import time
from typing import Callable

from webapp.ai_copy.contracts import ProductReference


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    reference: ProductReference
    stored_at: float


class ProductReferenceCache:
    """Keeps recent successful lookups and an older fallback for transient outages."""

    def __init__(
        self,
        *,
        fresh_seconds: float,
        stale_seconds: float,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fresh_seconds = fresh_seconds
        self._stale_seconds = max(stale_seconds, fresh_seconds)
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = Lock()

    def get_fresh(self, key: str) -> ProductReference | None:
        return self._get(key, max_age=self._fresh_seconds)

    def get_stale(self, key: str) -> ProductReference | None:
        return self._get(key, max_age=self._stale_seconds)

    def put(self, key: str, reference: ProductReference) -> None:
        entry = _CacheEntry(reference.model_copy(deep=True), self._clock())
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def _get(self, key: str, *, max_age: float) -> ProductReference | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now - entry.stored_at > self._stale_seconds:
                del self._entries[key]
                return None
            if now - entry.stored_at > max_age:
                return None
            self._entries.move_to_end(key)
            return entry.reference.model_copy(deep=True)

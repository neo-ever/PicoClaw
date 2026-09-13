"""A small thread-safe LRU cache for selector scores."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock


class ScoreCache:
    """Store scores by content hash without retaining repository text."""

    def __init__(self, max_entries: int = 2_048) -> None:
        if max_entries < 1:
            raise ValueError("cache max_entries must be positive")
        self.max_entries = max_entries
        self._values: OrderedDict[str, float] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> tuple[bool, float | None]:
        with self._lock:
            if key not in self._values:
                return False, None
            value = self._values.pop(key)
            self._values[key] = value
            return True, value

    def put(self, key: str, value: float) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._values[key] = value
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

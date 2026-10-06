"""Rolling per-pod history, keyed by pod UID so a replaced pod starts clean."""
from __future__ import annotations

from collections import deque

from .models import PodSnapshot


class MetricsWindow:
    def __init__(self, size: int = 200):
        self._size = size
        self._by_uid: dict[str, deque[PodSnapshot]] = {}

    def add(self, snap: PodSnapshot) -> None:
        self._by_uid.setdefault(snap.uid, deque(maxlen=self._size)).append(snap)

    def history(self, uid: str) -> list[PodSnapshot]:
        return list(self._by_uid.get(uid, ()))

    def prune(self, live_uids: set[str]) -> None:
        for uid in set(self._by_uid) - live_uids:
            del self._by_uid[uid]

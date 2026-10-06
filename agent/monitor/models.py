"""Immutable observations the rest of the agent reasons over."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PodSnapshot:
    """One pod's state at one poll. Rules read these; they never touch the cluster."""
    uid: str
    namespace: str
    name: str
    phase: str
    ready: bool
    restart_count: int
    waiting_reason: str | None          # e.g. CrashLoopBackOff, ImagePullBackOff
    last_terminated_reason: str | None  # e.g. OOMKilled, Error
    last_terminated_exit_code: int | None
    observed_at: datetime
    # Populated by the metrics layer in Phase 5 (needs metrics-server); None until then.
    labels: dict = field(default_factory=dict, compare=False)
    memory_bytes: int | None = None
    cpu_millicores: int | None = None

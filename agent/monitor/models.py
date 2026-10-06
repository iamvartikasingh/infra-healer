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
    labels: dict = field(default_factory=dict, compare=False)
    # Filled from the Metrics API (needs metrics-server); None when unavailable.
    memory_bytes: int | None = None
    cpu_millicores: int | None = None
    metrics_timestamp: datetime | None = None  # when the sample was TAKEN (not polled)
    memory_limit_bytes: int | None = None

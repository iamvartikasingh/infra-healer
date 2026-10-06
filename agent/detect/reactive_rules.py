"""Deterministic detection of failures that have already happened.

Pure functions: PodSnapshot in, Findings out. No I/O, no clock, no state, so
they are trivially testable. Edge-triggering (emit once per incident) is the
watcher's job, using Finding.dedupe_key.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from agent.monitor.models import PodSnapshot


class FindingKind(str, Enum):
    CRASH_LOOP = "CRASH_LOOP"
    OOM_KILLED = "OOM_KILLED"
    RESTART_THRESHOLD = "RESTART_THRESHOLD"


class Severity(str, Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    severity: Severity
    namespace: str
    pod: str
    uid: str
    detail: str
    observed_at: datetime
    dedupe_key: str


def _finding(s: PodSnapshot, kind: FindingKind, sev: Severity, detail: str, key_extra: str = "") -> Finding:
    return Finding(kind, sev, s.namespace, s.name, s.uid, detail, s.observed_at,
                   dedupe_key=f"{s.uid}:{kind.value}{':' + key_extra if key_extra else ''}")


def detect_crash_loop(s: PodSnapshot) -> Finding | None:
    if s.waiting_reason == "CrashLoopBackOff":
        return _finding(s, FindingKind.CRASH_LOOP, Severity.CRITICAL,
                        f"CrashLoopBackOff after {s.restart_count} restarts "
                        f"(last exit code {s.last_terminated_exit_code})")
    return None


def detect_oom_killed(s: PodSnapshot) -> Finding | None:
    if s.last_terminated_reason == "OOMKilled":
        # restart_count in the key: each new OOM kill is a new event, but the
        # same lingering lastState across polls is not.
        return _finding(s, FindingKind.OOM_KILLED, Severity.CRITICAL,
                        f"container OOMKilled (restart #{s.restart_count})", str(s.restart_count))
    return None


def detect_restart_threshold(s: PodSnapshot, threshold: int = 3) -> Finding | None:
    if s.restart_count >= threshold:
        return _finding(s, FindingKind.RESTART_THRESHOLD, Severity.WARNING,
                        f"{s.restart_count} restarts (threshold {threshold})")
    return None


def evaluate(s: PodSnapshot, restart_threshold: int = 3) -> list[Finding]:
    checks = (detect_crash_loop(s), detect_oom_killed(s), detect_restart_threshold(s, restart_threshold))
    return [f for f in checks if f is not None]

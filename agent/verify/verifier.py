"""After a remediation: wait, then check the workload is actually healthy."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from agent.detect.reactive_rules import Severity, evaluate
from agent.monitor.watcher import snapshot_from_pod

# Labels that identify one generation/replica rather than the workload.
_VOLATILE = {"pod-template-hash", "controller-revision-hash", "statefulset.kubernetes.io/pod-name"}


def workload_selector(labels: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in _VOLATILE)


@dataclass(frozen=True)
class VerifyResult:
    healthy: bool
    detail: str


class Verifier:
    def __init__(self, core_api, stabilization_seconds: float = 20.0, timeout_seconds: float = 120.0,
                 poll_seconds: float = 3.0, consecutive_ok: int = 2,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._api, self._stab, self._timeout = core_api, stabilization_seconds, timeout_seconds
        self._poll, self._need_ok = poll_seconds, consecutive_ok
        self._sleep, self._clock = sleep, clock

    def check_once(self, namespace: str, selector: str) -> VerifyResult:
        pods = [p for p in self._api.list_namespaced_pod(namespace, label_selector=selector).items
                if not getattr(p.metadata, "deletion_timestamp", None)]  # ignore pods being torn down
        if not pods:
            return VerifyResult(False, "no running pods for workload")
        now = datetime.now(timezone.utc)
        problems = []
        for p in pods:
            s = snapshot_from_pod(p, now)
            crit = [f.kind.value for f in evaluate(s) if f.severity is Severity.CRITICAL]
            if crit:
                problems.append(f"{s.name}: {','.join(crit)}")
            elif not s.ready:
                problems.append(f"{s.name}: not ready")
        return VerifyResult(not problems, "; ".join(problems) or f"{len(pods)} pod(s) ready")

    def verify(self, namespace: str, selector: str) -> VerifyResult:
        """Stabilization wait, then require `consecutive_ok` healthy checks in a row before the deadline.
        A single healthy blip (a crash-looping pod between restarts) is not recovery."""
        self._sleep(self._stab)
        deadline, ok, last = self._clock() + self._timeout, 0, VerifyResult(False, "not checked")
        while True:
            last = self.check_once(namespace, selector)
            ok = ok + 1 if last.healthy else 0
            if ok >= self._need_ok:
                return last
            if self._clock() >= deadline:
                return VerifyResult(False, f"unhealthy at timeout: {last.detail}")
            self._sleep(self._poll)

"""Polls pod status and turns it into edge-triggered findings.

    python -m agent.monitor.watcher --namespace demo [--once]
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from datetime import datetime, timezone
from typing import Callable

from kubernetes import client, config

from kubernetes.utils import parse_quantity

from agent.detect.predictive_rules import PredictiveConfig, evaluate_predictive
from agent.detect.reactive_rules import Finding, Severity, evaluate
from agent.monitor.metrics import MetricsSource, PodMetrics
from agent.monitor.models import PodSnapshot
from agent.monitor.window import MetricsWindow

log = logging.getLogger("watcher")


def _memory_limit(pod) -> int | None:
    """Sum of container memory limits; None if any container is unlimited (no meaningful ceiling)."""
    containers = getattr(getattr(pod, "spec", None), "containers", None) or []
    total = 0
    for c in containers:
        lim = ((getattr(c, "resources", None) and c.resources.limits) or {}).get("memory")
        if not lim:
            return None
        total += int(parse_quantity(lim))
    return total or None


def snapshot_from_pod(pod, now: datetime | None = None, metrics: PodMetrics | None = None) -> PodSnapshot:
    """Flatten a V1Pod into a PodSnapshot. Uses the worst-off container."""
    statuses = pod.status.container_statuses or []
    waiting = next((cs.state.waiting.reason for cs in statuses
                    if cs.state and cs.state.waiting and cs.state.waiting.reason), None)
    last = next((cs.last_state.terminated for cs in statuses
                 if cs.last_state and cs.last_state.terminated), None)
    return PodSnapshot(
        uid=pod.metadata.uid,
        namespace=pod.metadata.namespace,
        name=pod.metadata.name,
        phase=pod.status.phase or "Unknown",
        ready=bool(statuses) and all(cs.ready for cs in statuses),
        restart_count=sum(cs.restart_count for cs in statuses),
        waiting_reason=waiting,
        last_terminated_reason=last.reason if last else None,
        last_terminated_exit_code=last.exit_code if last else None,
        observed_at=now or datetime.now(timezone.utc),
        labels=dict(getattr(pod.metadata, "labels", None) or {}),
        memory_bytes=metrics.memory_bytes if metrics else None,
        cpu_millicores=metrics.cpu_millicores if metrics else None,
        metrics_timestamp=metrics.timestamp if metrics else None,
        memory_limit_bytes=_memory_limit(pod),
    )


class PodWatcher:
    def __init__(self, core_api, namespace: str, label_selector: str | None = None,
                 restart_threshold: int = 3, clear_after_seconds: float = 30.0,
                 window: MetricsWindow | None = None, metrics: MetricsSource | None = None,
                 predictive: PredictiveConfig | None = None,
                 clock: Callable[[], datetime] | None = None,
                 on_snapshot: Callable[[PodSnapshot], None] | None = None):
        self._api = core_api
        self._on_snapshot = on_snapshot
        self._ns = namespace
        self._selector = label_selector
        self._threshold = restart_threshold
        self.window = window or MetricsWindow()
        self._metrics = metrics
        self._predictive = predictive or PredictiveConfig(restart_threshold=restart_threshold)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._clear_after = clear_after_seconds
        # dedupe key -> last time the condition was seen. A key stays "active"
        # until unseen for clear_after_seconds, because CrashLoopBackOff flickers
        # off while the container restarts and must not read as a new incident.
        # Time-based (not poll-count) so behavior is independent of poll interval.
        self._last_seen: dict[str, datetime] = {}

    def poll_once(self) -> list[Finding]:
        """Return findings that are NEW since the last poll (rising edge only)."""
        pods = self._api.list_namespaced_pod(self._ns, label_selector=self._selector).items
        now = self._clock()
        by_name = self._metrics.fetch(self._ns) if self._metrics else {}
        current: dict[str, Finding] = {}
        live: set[str] = set()
        for pod in pods:
            snap = snapshot_from_pod(pod, now, by_name.get(pod.metadata.name))
            live.add(snap.uid)
            self.window.add(snap)
            if self._on_snapshot:
                self._on_snapshot(snap)
            reactive = evaluate(snap, self._threshold)
            for f in reactive:
                current[f.dedupe_key] = f
            # A pod that is already failing is the reactive rules' business; don't also "predict" it.
            if not any(f.severity is Severity.CRITICAL for f in reactive):
                for f in evaluate_predictive(self.window.history(snap.uid), self._predictive):
                    current[f.dedupe_key] = f
        self.window.prune(live)
        new = [f for k, f in current.items() if k not in self._last_seen]
        for k in current:
            self._last_seen[k] = now
        for k in [k for k, t in self._last_seen.items()
                  if (now - t).total_seconds() >= self._clear_after]:
            del self._last_seen[k]  # truly cleared; may fire again later
        return new

    def run(self, on_finding: Callable[[Finding], None], interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                for f in self.poll_once():
                    on_finding(f)
            except Exception:  # API blips must not kill the loop
                log.exception("poll failed; will retry")
            stop.wait(interval)


def _load_kube() -> None:
    try:
        config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="demo")
    p.add_argument("--selector", default=None)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--restart-threshold", type=int, default=3)
    p.add_argument("--once", action="store_true", help="poll once and exit")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    _load_kube()
    w = PodWatcher(client.CoreV1Api(), a.namespace, a.selector, a.restart_threshold)
    emit = lambda f: print(json.dumps({**f.__dict__, "kind": f.kind.value, "severity": f.severity.value,
                                       "observed_at": f.observed_at.isoformat()}), flush=True)
    if a.once:
        for f in w.poll_once():
            emit(f)
        return
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    w.run(emit, a.interval, stop)


if __name__ == "__main__":
    main()

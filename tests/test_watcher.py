from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from agent.detect.reactive_rules import FindingKind
from agent.monitor.watcher import PodWatcher, snapshot_from_pod


def pod(uid="u1", restarts=0, waiting=None, last=None, ready=True):
    cs = NS(ready=ready, restart_count=restarts,
            state=NS(waiting=NS(reason=waiting) if waiting else None),
            last_state=NS(terminated=NS(reason=last[0], exit_code=last[1]) if last else None))
    return NS(metadata=NS(uid=uid, namespace="demo", name=f"pod-{uid}"),
              status=NS(phase="Running", container_statuses=[cs]))


class Clock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeApi:
    def __init__(self):
        self.pods = []
        self.fail = False

    def list_namespaced_pod(self, ns, label_selector=None):
        if self.fail:
            raise RuntimeError("api down")
        return NS(items=self.pods)


def test_snapshot_extraction():
    s = snapshot_from_pod(pod(restarts=4, waiting="CrashLoopBackOff", last=("Error", 1), ready=False))
    assert (s.restart_count, s.waiting_reason, s.last_terminated_reason,
            s.last_terminated_exit_code, s.ready) == (4, "CrashLoopBackOff", "Error", 1, False)


def test_snapshot_handles_pod_without_container_statuses():
    p = NS(metadata=NS(uid="x", namespace="demo", name="x"),
           status=NS(phase="Pending", container_statuses=None))
    s = snapshot_from_pod(p)
    assert s.restart_count == 0 and s.ready is False and s.waiting_reason is None


def test_findings_are_edge_triggered():
    api = FakeApi()
    w = PodWatcher(api, "demo")
    api.pods = [pod(waiting="CrashLoopBackOff", restarts=1)]
    assert [f.kind for f in w.poll_once()] == [FindingKind.CRASH_LOOP]
    assert w.poll_once() == []  # same condition, no re-emit


def test_condition_clearing_allows_refire():
    api, clock = FakeApi(), Clock()
    w = PodWatcher(api, "demo", clear_after_seconds=30, clock=clock)
    api.pods = [pod(waiting="CrashLoopBackOff")]
    w.poll_once()
    api.pods = [pod()]
    clock.advance(10)
    assert w.poll_once() == []
    clock.advance(25)  # unseen for 35s >= 30s -> cleared
    assert w.poll_once() == []
    api.pods = [pod(waiting="CrashLoopBackOff")]
    clock.advance(1)
    assert len(w.poll_once()) == 1


def test_flapping_condition_does_not_refire():
    # CrashLoopBackOff flickers off between restart attempts.
    api, clock = FakeApi(), Clock()
    w = PodWatcher(api, "demo", clear_after_seconds=30, clock=clock)
    api.pods = [pod(waiting="CrashLoopBackOff")]
    assert len(w.poll_once()) == 1
    for _ in range(5):
        api.pods = [pod()]
        clock.advance(8)
        assert w.poll_once() == []
        api.pods = [pod(waiting="CrashLoopBackOff")]
        clock.advance(8)
        assert w.poll_once() == []


def test_new_oom_kill_fires_again():
    api = FakeApi()
    w = PodWatcher(api, "demo")
    api.pods = [pod(restarts=1, last=("OOMKilled", 137))]
    assert len(w.poll_once()) == 1
    api.pods = [pod(restarts=2, last=("OOMKilled", 137))]
    assert FindingKind.OOM_KILLED in {f.kind for f in w.poll_once()}


def test_window_tracks_and_prunes_by_uid():
    api = FakeApi()
    w = PodWatcher(api, "demo")
    api.pods = [pod(uid="a")]
    w.poll_once(); w.poll_once()
    assert len(w.window.history("a")) == 2
    api.pods = [pod(uid="b")]
    w.poll_once()
    assert w.window.history("a") == [] and len(w.window.history("b")) == 1


def test_run_survives_api_errors():
    import threading
    api = FakeApi()
    api.fail = True
    w = PodWatcher(api, "demo")
    stop = threading.Event()
    calls = []
    def on_f(f):
        calls.append(f)
    # first poll raises; flip to healthy and stop after
    orig = api.list_namespaced_pod
    n = {"i": 0}
    def flaky(ns, label_selector=None):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")
        stop.set()
        return NS(items=[pod(waiting="CrashLoopBackOff")])
    api.list_namespaced_pod = flaky
    w.run(on_f, 0.01, stop)
    assert len(calls) == 1

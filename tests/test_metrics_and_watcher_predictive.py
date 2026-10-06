from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from kubernetes.client.rest import ApiException

from agent.detect.reactive_rules import FindingKind
from agent.monitor.metrics import MetricsSource
from agent.monitor.watcher import PodWatcher, _memory_limit, snapshot_from_pod
from tests.test_watcher import Clock, FakeApi, pod

MIB = 1024 * 1024


def item(name="pod-u1", mem="22604Ki", cpu="1549629n", ts="2026-10-06T03:22:47Z"):
    return {"metadata": {"name": name}, "timestamp": ts,
            "containers": [{"name": "a", "usage": {"cpu": cpu, "memory": mem}},
                           {"name": "b", "usage": {"cpu": "500000n", "memory": "1Mi"}}]}


class Custom:
    def __init__(self, items=None, exc=None):
        self.items, self.exc, self.calls = items or [], exc, 0

    def list_namespaced_custom_object(self, *a):
        self.calls += 1
        if self.exc:
            raise self.exc
        return {"items": self.items}


def test_metrics_parse_and_sum_containers():
    m = MetricsSource(Custom([item()])).fetch("demo")["pod-u1"]
    assert m.memory_bytes == 22604 * 1024 + MIB and m.cpu_millicores == 2
    assert m.timestamp == datetime(2026, 10, 6, 3, 22, 47, tzinfo=timezone.utc)


def test_metrics_server_missing_degrades_to_empty_and_warns_once(caplog):
    src = MetricsSource(Custom(exc=ApiException(status=503)))
    assert src.fetch("demo") == {} and src.fetch("demo") == {}
    assert sum("metrics unavailable" in r.message for r in caplog.records) == 1


def test_malformed_item_is_skipped_not_fatal():
    out = MetricsSource(Custom([{"metadata": {"name": "bad"}}, item()])).fetch("demo")
    assert list(out) == ["pod-u1"]


def with_limits(p, *limits):
    p.spec = NS(containers=[NS(resources=NS(limits={"memory": l} if l else None)) for l in limits])
    return p


def test_memory_limit_sums_containers_and_none_if_any_unlimited():
    assert _memory_limit(with_limits(pod(), "64Mi")) == 64 * MIB
    assert _memory_limit(with_limits(pod(), "64Mi", "32Mi")) == 96 * MIB
    assert _memory_limit(with_limits(pod(), "64Mi", None)) is None
    assert _memory_limit(pod()) is None  # pod object without a spec


class Metrics:
    """Scripted metrics source: memory climbs 200KiB/s, a new sample every 10s."""
    def __init__(self, clock):
        self.clock, self.t0 = clock, clock()

    def fetch(self, ns):
        el = (self.clock() - self.t0).total_seconds()
        ts = self.t0 + timedelta(seconds=(el // 10) * 10)
        from agent.monitor.metrics import PodMetrics
        return {"pod-u1": PodMetrics(int(20 * MIB + 200 * 1024 * (el // 10) * 10), 5, ts)}


def run_watcher(secs, poll=2, **podkw):
    api, clock = FakeApi(), Clock()
    p = with_limits(pod(**podkw), "64Mi")
    api.pods = [p]
    w = PodWatcher(api, "demo", clock=clock, metrics=Metrics(clock))
    found = []
    for _ in range(secs // poll):
        found += w.poll_once()
        clock.advance(poll)
    return found, w


def test_watcher_emits_predicted_oom_once_before_any_failure():
    found, w = run_watcher(120)
    kinds = [f.kind for f in found]
    assert kinds == [FindingKind.PREDICTED_OOM]  # exactly once, no reactive findings
    assert found[0].predictive and found[0].confidence > 0


def test_watcher_emits_nothing_without_enough_history():
    found, _ = run_watcher(30)
    assert found == []


def test_predictive_is_suppressed_when_pod_already_failing_reactively():
    found, _ = run_watcher(120, waiting="CrashLoopBackOff", restarts=1)
    assert FindingKind.PREDICTED_OOM not in [f.kind for f in found]
    assert FindingKind.CRASH_LOOP in [f.kind for f in found]


def test_snapshot_carries_metrics():
    from agent.monitor.metrics import PodMetrics
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = snapshot_from_pod(with_limits(pod(), "64Mi"), metrics=PodMetrics(5 * MIB, 7, ts))
    assert (s.memory_bytes, s.cpu_millicores, s.metrics_timestamp, s.memory_limit_bytes) == (5 * MIB, 7, ts, 64 * MIB)

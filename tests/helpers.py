from datetime import datetime, timezone

from agent.monitor.models import PodSnapshot

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snap(**kw) -> PodSnapshot:
    base = dict(uid="u1", namespace="demo", name="p1", phase="Running", ready=True,
                restart_count=0, waiting_reason=None, last_terminated_reason=None,
                last_terminated_exit_code=None, observed_at=T0)
    base.update(kw)
    return PodSnapshot(**base)


from datetime import timedelta  # noqa: E402

MIB = 1024 * 1024


def mem_series(n=8, start=20 * MIB, rate=200 * 1024, step=10, limit=64 * MIB, noise=None, restarts=None,
               poll_dupes=1, **kw):
    """History of snapshots with memory growing `rate` bytes/s, one metrics sample every `step`s.
    `poll_dupes` > 1 repeats each sample (the watcher polls faster than metrics-server updates).
    `restarts` maps sample index -> restart_count from that sample on."""
    out = []
    for i in range(n):
        mem = int(start + rate * i * step + (noise[i % len(noise)] if noise else 0))
        rc = max([c for idx, c in (restarts or {}).items() if i >= idx], default=0)
        for d in range(poll_dupes):
            out.append(snap(memory_bytes=mem, memory_limit_bytes=limit, metrics_timestamp=T0 + timedelta(seconds=i * step),
                            observed_at=T0 + timedelta(seconds=i * step + d * 2), restart_count=rc, **kw))
    return out

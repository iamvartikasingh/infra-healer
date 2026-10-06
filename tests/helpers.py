from datetime import datetime, timezone

from agent.monitor.models import PodSnapshot

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snap(**kw) -> PodSnapshot:
    base = dict(uid="u1", namespace="demo", name="p1", phase="Running", ready=True,
                restart_count=0, waiting_reason=None, last_terminated_reason=None,
                last_terminated_exit_code=None, observed_at=T0)
    base.update(kw)
    return PodSnapshot(**base)

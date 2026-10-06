"""Builds the structured incident context the LLM reasons over."""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel

from agent.detect.reactive_rules import Finding
from agent.monitor.models import PodSnapshot

log = logging.getLogger("diagnose.context")

MAX_LOG_CHARS = 4000
MAX_EVENT_MSG = 300
MAX_EVENTS = 15


class EventInfo(BaseModel):
    type: str | None = None
    reason: str | None = None
    message: str = ""
    count: int | None = None


class HistoryPoint(BaseModel):
    observed_at: datetime
    restart_count: int
    ready: bool
    waiting_reason: str | None = None


class IncidentContext(BaseModel):
    finding: dict
    pod: dict
    history: list[HistoryPoint] = []
    events: list[EventInfo] = []
    log_tail: str = ""
    previous_log_tail: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def _jsonable(obj) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(obj), default=lambda o: o.value if hasattr(o, "value") else o.isoformat()))


def _tail(text: str | None) -> str:
    return (text or "")[-MAX_LOG_CHARS:]


def collect_context(core_api, finding: Finding, snapshot: PodSnapshot,
                    history: list[PodSnapshot] | None = None) -> IncidentContext:
    """Gather events and logs. Every I/O call degrades to empty rather than raising:
    a partially-informed diagnosis (with lower confidence) beats no diagnosis."""
    ns, name = finding.namespace, finding.pod
    events: list[EventInfo] = []
    try:
        items = core_api.list_namespaced_event(ns, field_selector=f"involvedObject.name={name}").items
        # Oldest -> newest; events with no timestamp sort first.
        items.sort(key=lambda e: (e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc)))
        for e in items[-MAX_EVENTS:]:
            events.append(EventInfo(type=e.type, reason=e.reason, count=e.count,
                                    message=(e.message or "")[:MAX_EVENT_MSG]))
    except Exception:
        log.warning("could not read events for %s/%s", ns, name, exc_info=True)

    def logs(previous: bool) -> str:
        try:
            # kubernetes client 36 str()s the bytes body ("b'...\\n'"), so take the raw response.
            resp = core_api.read_namespaced_pod_log(name, ns, tail_lines=60, previous=previous,
                                                    _preload_content=False)
            return _tail(resp.data.decode("utf-8", errors="replace"))
        except Exception:  # e.g. 400 when there is no previous container
            return ""

    return IncidentContext(
        finding=_jsonable(finding),
        pod=_jsonable(snapshot),
        history=[HistoryPoint(observed_at=h.observed_at, restart_count=h.restart_count,
                              ready=h.ready, waiting_reason=h.waiting_reason) for h in (history or [])[-20:]],
        events=events,
        log_tail=logs(False),
        previous_log_tail=logs(True) if snapshot.restart_count > 0 else "",
    )

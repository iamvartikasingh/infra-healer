from datetime import datetime, timezone
from types import SimpleNamespace as NS

from agent.detect.reactive_rules import evaluate
from agent.diagnose.context import MAX_EVENTS, MAX_LOG_CHARS, collect_context
from tests.helpers import snap

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Api:
    def __init__(self, events=(), log=b"hello\n", fail_previous=False, fail_all=False):
        self.events, self.log, self.fail_previous, self.fail_all = list(events), log, fail_previous, fail_all
        self.log_kwargs = []

    def list_namespaced_event(self, ns, field_selector=None):
        if self.fail_all:
            raise RuntimeError("boom")
        return NS(items=self.events)

    def read_namespaced_pod_log(self, name, ns, **kw):
        self.log_kwargs.append(kw)
        if self.fail_all or (kw.get("previous") and self.fail_previous):
            raise RuntimeError("no previous container")
        return NS(data=self.log)


def finding_and_snap(**kw):
    s = snap(waiting_reason="CrashLoopBackOff", restart_count=2, **kw)
    return evaluate(s)[0], s


def ev(i, msg="m"):
    return NS(type="Warning", reason="R", message=msg, count=1, last_timestamp=T.replace(second=i), event_time=None)


def test_logs_are_decoded_text_not_bytes_repr():
    # Regression: kubernetes client 36 str()s bytes -> "b'...'"; we must read the raw body.
    f, s = finding_and_snap()
    ctx = collect_context(Api(log="ünïcode ok\n".encode()), f, s)
    assert ctx.log_tail == "ünïcode ok\n"


def test_requests_raw_response():
    api = Api()
    f, s = finding_and_snap()
    collect_context(api, f, s)
    assert all(kw["_preload_content"] is False for kw in api.log_kwargs)


def test_invalid_utf8_does_not_crash():
    f, s = finding_and_snap()
    assert "�" in collect_context(Api(log=b"ok \xff\xfe"), f, s).log_tail


def test_log_truncated_to_tail():
    f, s = finding_and_snap()
    ctx = collect_context(Api(log=b"a" * 10000 + b"END"), f, s)
    assert len(ctx.log_tail) == MAX_LOG_CHARS and ctx.log_tail.endswith("END")


def test_events_capped_sorted_and_message_truncated():
    f, s = finding_and_snap()
    events = [ev(i % 60, "x" * 1000) for i in range(30, 0, -1)]
    ctx = collect_context(Api(events=events), f, s)
    assert len(ctx.events) == MAX_EVENTS and all(len(e.message) <= 300 for e in ctx.events)


def test_missing_previous_logs_degrades_to_empty():
    f, s = finding_and_snap()
    ctx = collect_context(Api(fail_previous=True), f, s)
    assert ctx.previous_log_tail == "" and ctx.log_tail == "hello\n"


def test_total_api_failure_still_returns_context():
    f, s = finding_and_snap()
    ctx = collect_context(Api(fail_all=True), f, s)
    assert ctx.events == [] and ctx.log_tail == "" and ctx.finding["kind"] == "CRASH_LOOP"


def test_history_included_and_capped():
    f, s = finding_and_snap()
    ctx = collect_context(Api(), f, s, history=[snap(restart_count=i) for i in range(50)])
    assert len(ctx.history) == 40 and ctx.history[-1].restart_count == 49

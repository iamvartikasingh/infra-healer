import json
import re
import sqlite3
from pathlib import Path

import pytest

from agent.state.machine import State
from agent.state.store import Store
from dashboard.app import INDEX, create_app, incident_view, sse_stream

S = State


def make_store():
    st = Store()
    iid = st.create_incident("u1", "demo", "pod1", "app=x", "CRASH_LOOP", "CrashLoopBackOff after 2 restarts")
    return st, iid


def to_pending(st, iid, action="RESTART_POD", conf=0.6):
    dx = json.dumps({"rootCause": "marker", "confidence": conf, "recommendedAction": action, "reasoning": "logs"})
    pol = json.dumps({"outcome": "REQUIRE_APPROVAL", "rule": "LOW_CONFIDENCE", "reason": "r", "action": action,
                      "confidence": conf, "mode": "AUTONOMOUS", "threshold": 0.8})
    st.transition(iid, S.DIAGNOSING, "asking")
    st.transition(iid, S.ACTION_PROPOSED, "proposed", approval="PENDING", diagnosis_json=dx, policy_json=pol)


@pytest.fixture
def ctx():
    st, iid = make_store()
    return st, iid, create_app(st).test_client()


JSON = {"Content-Type": "application/json"}


# ---------------- read API ----------------
def test_incident_list_has_parsed_views_and_stream_cursor(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    j = c.get("/api/incidents").json
    v = j["incidents"][0]
    assert j["seq"] == st.max_seq() and v["id"] == iid and v["can_decide"] is True
    assert v["diagnosis"]["recommendedAction"] == "RESTART_POD" and v["policy"]["threshold"] == 0.8
    assert [e["to"] for e in v["events"]] == ["DETECTED", "DIAGNOSING", "ACTION_PROPOSED"]


def test_predictive_flag_and_confidence_exposed():
    st = Store()
    iid = st.create_incident("u", "d", "p", "s", "PREDICTED_OOM", "memory rising", confidence=0.7)
    v = incident_view(st, st.get(iid))
    assert v["finding"]["predictive"] is True and v["finding"]["confidence"] == 0.7 and v["diagnosis"] is None


def test_corrupt_json_in_db_does_not_break_the_view():
    st, iid = make_store()
    st._db.execute("UPDATE incidents SET diagnosis_json='{not json', policy_json='' WHERE id=?", (iid,))
    v = incident_view(st, st.get(iid))
    assert v["diagnosis"] is None and v["policy"] is None


def test_index_page_served(ctx):
    r = ctx[2].get("/")
    assert r.status_code == 200 and b"InfraHealer" in r.data and r.mimetype == "text/html"


def test_security_headers(ctx):
    h = ctx[2].get("/api/incidents").headers
    assert h["Cache-Control"] == "no-store" and "default-src 'none'" in h["Content-Security-Policy"]


# ---------------- decisions ----------------
def test_approve_records_decision(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    assert c.post(f"/api/incidents/{iid}/approve", headers=JSON, json={}).status_code == 200
    assert st.get(iid)["approval"] == "APPROVED"


def test_reject_records_decision(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    assert c.post(f"/api/incidents/{iid}/reject", headers=JSON, json={}).status_code == 200
    assert st.get(iid)["approval"] == "REJECTED"


def test_cannot_decide_twice_or_flip(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    c.post(f"/api/incidents/{iid}/approve", headers=JSON, json={})
    r = c.post(f"/api/incidents/{iid}/reject", headers=JSON, json={})
    assert r.status_code == 409 and st.get(iid)["approval"] == "APPROVED"


def test_cannot_approve_an_incident_that_is_not_pending(ctx):
    st, iid, c = ctx  # still DETECTED
    assert c.post(f"/api/incidents/{iid}/approve", headers=JSON, json={}).status_code == 409
    assert st.get(iid)["approval"] == "NONE"


def test_unknown_incident_is_404(ctx):
    assert ctx[2].post("/api/incidents/nope/approve", headers=JSON, json={}).status_code == 404


def test_decision_cannot_resurrect_a_closed_incident(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    st.transition(iid, S.ESCALATED, "superseded", approval="NONE")
    assert c.post(f"/api/incidents/{iid}/approve", headers=JSON, json={}).status_code == 409


# ---------------- guards ----------------
def test_form_post_is_refused_cross_site_forms_cannot_send_json(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    r = c.post(f"/api/incidents/{iid}/approve", data={"x": "1"})  # form-encoded
    assert r.status_code == 415 and st.get(iid)["approval"] == "PENDING"


def test_cross_origin_post_is_refused(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    r = c.post(f"/api/incidents/{iid}/approve", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and st.get(iid)["approval"] == "PENDING"


def test_same_origin_post_allowed(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    r = c.post(f"/api/incidents/{iid}/approve", json={}, headers={"Origin": "http://localhost"})
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "169.254.169.254", "localhost.evil.example"])
def test_dns_rebinding_hosts_are_refused_even_for_reads(ctx, host):
    # attacker.example resolves to 127.0.0.1; Host/Origin match each other, so only a Host allowlist stops it
    assert ctx[2].get("/api/incidents", headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000", "[::1]"])
def test_local_hosts_allowed(ctx, host):
    assert ctx[2].get("/api/incidents", headers={"Host": host}).status_code == 200


def test_rebinding_post_with_matching_origin_still_refused(ctx):
    st, iid, c = ctx
    to_pending(st, iid)
    r = c.post(f"/api/incidents/{iid}/approve", json={}, headers={"Host": "evil.example", "Origin": "http://evil.example"})
    assert r.status_code == 400 and st.get(iid)["approval"] == "PENDING"


# ---------------- SSE ----------------
def run_stream(st, since=0, ticks=1, **kw):
    frames, n = [], {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > ticks

    for f in sse_stream(st, since, sleep=lambda s: None, should_stop=stop, **kw):
        frames.append(f)
    return frames


def data_of(frame):
    return json.loads(re.search(r"^data: (.*)$", frame, re.M).group(1))


def test_stream_starts_with_retry_and_emits_changed_incidents_once():
    st, iid = make_store()
    st.transition(iid, S.DIAGNOSING, "a")
    frames = run_stream(st, since=0)
    assert frames[0].startswith("retry:")
    inc = [f for f in frames if "event: incident" in f]
    assert len(inc) == 1  # two events, one incident -> one frame carrying the latest view
    assert data_of(inc[0])["state"] == "DIAGNOSING"
    assert f"id: {st.max_seq()}" in inc[0]


def test_stream_resumes_after_cursor_without_replaying():
    st, iid = make_store()
    cursor = st.max_seq()
    assert [f for f in run_stream(st, since=cursor) if "event: incident" in f] == []
    st.transition(iid, S.DIAGNOSING, "b")
    assert len([f for f in run_stream(st, since=cursor) if "event: incident" in f]) == 1


def test_stream_covers_multiple_incidents_in_order():
    st, a = make_store()
    b = st.create_incident("u2", "demo", "pod2", "app=x", "OOM_KILLED", "d")
    ids = [data_of(f)["id"] for f in run_stream(st) if "event: incident" in f]
    assert ids == [a, b]


def test_stream_heartbeat_when_idle():
    st, _ = make_store()
    t = iter(range(0, 1000, 20))
    frames = run_stream(st, since=st.max_seq(), ticks=2, heartbeat=15, clock=lambda: next(t))
    assert ": keepalive\n\n" in frames


def test_frame_is_single_line_data_even_with_newlines_in_notes():
    st, iid = make_store()
    st.add_note(iid, "line1\nline2\r\ndata: injected")
    frame = [f for f in run_stream(st) if "event: incident" in f][0]
    assert frame.count("\ndata:") == 1  # JSON-escaped; an attacker's note can't inject SSE fields
    assert "line1\nline2" in data_of(frame)["events"][-1]["note"]


def test_stream_route_serves_event_stream(ctx):
    st, iid, c = ctx
    r = c.get("/stream?since=0", buffered=False)
    assert r.mimetype == "text/event-stream"
    first = next(iter(r.response))
    assert first.startswith("retry:") if isinstance(first, str) else first.startswith(b"retry:")
    r.close()


# ---------------- front-end hygiene ----------------
def test_page_never_inserts_untrusted_text_as_html():
    src = INDEX.read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"):
        assert banned not in src, banned


def test_page_has_no_external_resources():
    src = INDEX.read_text()
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', src) and "@import" not in src


def test_status_is_never_color_only():
    src = INDEX.read_text()
    assert all(k in src for k in ("✓", "▲", "✕"))  # each status carries an icon + a text label


# ---------------- store: shared DB between agent and dashboard ----------------
def test_file_db_uses_wal_and_two_connections_interoperate(tmp_path):
    path = str(tmp_path / "t.db")
    agent, dash = Store(path), Store(path)
    iid = agent.create_incident("u", "d", "p", "s", "CRASH_LOOP", "x")
    to_pending(agent, iid)
    assert dash.decide_approval(iid, True) is True
    assert agent.get(iid)["approval"] == "APPROVED"
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_old_database_without_confidence_column_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE incidents (id TEXT PRIMARY KEY, pod_uid TEXT NOT NULL, namespace TEXT NOT NULL,
      pod TEXT NOT NULL, selector TEXT NOT NULL, finding_kind TEXT NOT NULL, finding_detail TEXT NOT NULL,
      state TEXT NOT NULL, approval TEXT NOT NULL DEFAULT 'NONE', diagnosis_json TEXT, policy_json TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);""")
    db.commit(); db.close()
    st = Store(path)
    assert st.create_incident("u", "d", "p", "s", "PREDICTED_OOM", "x", confidence=0.5)

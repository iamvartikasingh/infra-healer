"""Live incident dashboard: one Flask page + JSON API + Server-Sent Events.

It is a thin view over the SQLite state the agent already writes. The only thing
it can change is a human's approve/reject decision, through the same
`Store.decide_approval` the CLI uses (and that call is itself state-guarded).

    python -m dashboard.app --db infra-healer.db --port 8000

Security posture (no auth is in scope, so this is localhost-only by design):
  * binds 127.0.0.1 by default;
  * Host header allowlist -> blocks DNS-rebinding;
  * state-changing endpoints require JSON content-type and a same-origin Origin
    -> blocks cross-site form posts from other tabs.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Callable, Iterator

from flask import Flask, Response, abort, jsonify, request, send_file

from agent.state.store import Store

INDEX = Path(__file__).with_name("index.html")
DEFAULT_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _loads(s: str | None):
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


def _hostname(host_header: str) -> str:
    """'localhost:8000' -> 'localhost'; '[::1]:8000' -> '[::1]'."""
    return host_header.split("]")[0] + "]" if host_header.startswith("[") else host_header.split(":")[0]


def incident_view(store: Store, inc: dict) -> dict:
    return {
        "id": inc["id"], "namespace": inc["namespace"], "pod": inc["pod"], "state": inc["state"],
        "approval": inc["approval"], "created_at": inc["created_at"], "updated_at": inc["updated_at"],
        "finding": {"kind": inc["finding_kind"], "detail": inc["finding_detail"],
                    "confidence": inc.get("finding_confidence"),
                    "predictive": inc["finding_kind"].startswith("PREDICTED_")},
        "diagnosis": _loads(inc["diagnosis_json"]),
        "policy": _loads(inc["policy_json"]),
        "can_decide": inc["state"] == "ACTION_PROPOSED" and inc["approval"] == "PENDING",
        "events": [{"seq": e["seq"], "ts": e["ts"], "from": e["from_state"], "to": e["to_state"], "note": e["note"]}
                   for e in store.events(inc["id"])],
    }


def sse_stream(store: Store, since: int, poll: float = 1.0, heartbeat: float = 15.0,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               should_stop: Callable[[], bool] = lambda: False) -> Iterator[str]:
    """Yields SSE frames. Each change to an incident re-sends that incident's full view, so a
    client that missed frames (or reconnects with Last-Event-ID) converges without replay logic."""
    yield "retry: 3000\n\n"
    last, last_beat = since, clock()
    while not should_stop():
        events = store.events_after(last)
        if events:
            last = events[-1]["seq"]
            for iid in dict.fromkeys(e["incident_id"] for e in events):  # ordered, de-duplicated
                view = incident_view(store, store.get(iid))
                yield f"id: {last}\nevent: incident\ndata: {json.dumps(view)}\n\n"
            last_beat = clock()
        elif clock() - last_beat >= heartbeat:
            yield ": keepalive\n\n"  # keeps proxies from closing an idle stream
            last_beat = clock()
        sleep(poll)


def create_app(store: Store, allowed_hosts: frozenset[str] = DEFAULT_HOSTS) -> Flask:
    app = Flask(__name__)

    @app.before_request
    def _guard():
        if _hostname(request.host) not in allowed_hosts:
            abort(400, "host not allowed")  # DNS rebinding: attacker.example resolving to 127.0.0.1
        if request.method == "POST":
            if not request.is_json:
                abort(415, "JSON required")  # a cross-site <form> cannot send application/json
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                abort(403, "cross-origin request refused")

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'self' 'unsafe-inline'; "
                                                   "style-src 'unsafe-inline'; connect-src 'self'")
        return resp

    @app.get("/")
    def index():
        return send_file(INDEX, mimetype="text/html")

    @app.get("/api/incidents")
    def incidents():
        seq = store.max_seq()  # read BEFORE the rows so the stream can only replay, never skip
        rows = sorted(store.list_incidents(), key=lambda i: i["updated_at"], reverse=True)[:100]
        return jsonify(seq=seq, incidents=[incident_view(store, r) for r in rows])

    def _decide(iid: str, approve: bool):
        try:
            store.get(iid)
        except KeyError:
            abort(404)
        if not store.decide_approval(iid, approve):
            return jsonify(error="incident is not awaiting a decision"), 409
        return jsonify(ok=True)

    @app.post("/api/incidents/<iid>/approve")
    def approve(iid):
        return _decide(iid, True)

    @app.post("/api/incidents/<iid>/reject")
    def reject(iid):
        return _decide(iid, False)

    @app.get("/stream")
    def stream():
        since = request.headers.get("Last-Event-ID") or request.args.get("since", "0")
        since = int(since) if since.isdigit() else 0
        return Response(sse_stream(store, since), mimetype="text/event-stream",
                        headers={"X-Accel-Buffering": "no"})

    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="infra-healer.db")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    create_app(Store(a.db)).run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()

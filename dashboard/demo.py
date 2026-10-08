"""Public, session-isolated simulation. Never connects to Kubernetes or an LLM."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import OrderedDict

from flask import abort, g, jsonify, request, session
from werkzeug.local import LocalProxy

from agent.diagnose.fake import _BY_KIND
from agent.diagnose.schema import parse_diagnosis
from agent.policy.engine import PolicyConfig, evaluate
from agent.state.machine import State
from agent.state.store import Store
from dashboard.app import DEFAULT_HOSTS, create_app as dashboard_app


def create_app():
    # One process, bounded memory, one hour idle expiry. Evicted sessions restart empty.
    sessions = OrderedDict()
    lock = threading.RLock()

    def current_store():
        sid = session.get("demo_id")
        if not sid:
            sid = session["demo_id"] = secrets.token_hex(24)
        with lock:
            now = time.monotonic()
            for key in list(sessions):
                if now - sessions[key][1] > 3600:
                    sessions.pop(key)[0]._db.close()
            if sid not in sessions:
                if len(sessions) >= 64:
                    sessions.popitem(last=False)[1][0]._db.close()
                sessions[sid] = (Store(), now)
            store, _ = sessions[sid]
            sessions[sid] = (store, now)
            sessions.move_to_end(sid)
            return store

    hosts = DEFAULT_HOSTS | frozenset(filter(None, [os.getenv("RENDER_EXTERNAL_HOSTNAME")]))
    app = dashboard_app(LocalProxy(current_store), hosts)
    app.config.update(DEMO=True, SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")))

    @app.before_request
    def demo_lifecycle():
        # Serialize requests while they use a session store, including eviction.
        lock.acquire()
        g.demo_locked = True
        if request.path == "/stream":
            abort(404)  # public demo uses polling, so no long-lived worker allocation
        if request.path == "/api/incidents":
            st = current_store()
            for inc in st.list_incidents():
                iid, state = inc["id"], inc["state"]
                if state == "ACTION_PROPOSED" and inc["approval"] == "APPROVED":
                    st.transition(iid, State.REMEDIATING, "Simulation: replacing the pod; no cluster call is made")
                elif state == "ACTION_PROPOSED" and inc["approval"] == "REJECTED":
                    st.transition(iid, State.ESCALATED, "Human rejected the simulated action")
                elif state == "REMEDIATING":
                    st.transition(iid, State.VERIFYING, "Simulation: checking replacement pod readiness")
                elif state == "VERIFYING":
                    st.transition(iid, State.RESOLVED, "Simulation: replacement pod passed scripted health checks")

    @app.teardown_request
    def release_lock(error):
        # Earlier host/origin guards can abort before demo_lifecycle acquires it.
        if g.get("demo_locked", False):
            lock.release()

    @app.post("/api/demo/incidents")
    def inject():
        body = request.get_json(silent=True)
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind not in {"CRASH_LOOP", "OOM_KILLED", "PREDICTED_OOM"}:
            return jsonify(error="choose a supported scenario"), 400
        st = current_store()
        if len(st.list_incidents()) >= 20:
            return jsonify(error="Demo limit reached. Reset your demo to continue."), 429
        cause, confidence, action = _BY_KIND[kind]
        diagnosis = parse_diagnosis(json.dumps(dict(rootCause=cause, confidence=confidence,
                                                   recommendedAction=action, reasoning="Scripted demo diagnosis; no model called.")))
        decision = evaluate(diagnosis, PolicyConfig())
        iid = st.create_incident(secrets.token_hex(8), "simulation", "payments-demo", "app=payments-demo",
                                 kind, "Simulated scenario: " + cause, 0.75 if kind.startswith("PREDICTED") else None)
        st.transition(iid, State.DIAGNOSING, "Simulation: collecting scripted evidence")
        st.transition(iid, State.ACTION_PROPOSED, decision.reason, approval="PENDING",
                      diagnosis_json=diagnosis.model_dump_json(by_alias=True),
                      policy_json=json.dumps({**decision.to_json(), "mode": "HUMAN_APPROVAL", "threshold": 0.8}))
        return jsonify(id=iid), 201

    @app.post("/api/demo/reset")
    def reset():
        sid = session.get("demo_id")
        if sid in sessions:
            sessions.pop(sid)[0]._db.close()
        return jsonify(ok=True)

    return app

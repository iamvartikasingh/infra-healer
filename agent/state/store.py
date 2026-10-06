"""SQLite persistence: incidents, an append-only timeline, and execution claims."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from agent.state.machine import IllegalTransition, State, can_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, pod_uid TEXT NOT NULL, namespace TEXT NOT NULL, pod TEXT NOT NULL,
  selector TEXT NOT NULL, finding_kind TEXT NOT NULL, finding_detail TEXT NOT NULL,
  state TEXT NOT NULL, approval TEXT NOT NULL DEFAULT 'NONE',
  diagnosis_json TEXT, policy_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- at most one open incident per pod, enforced atomically by the database
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_pod ON incidents(pod_uid)
  WHERE state NOT IN ('RESOLVED','ESCALATED');
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, ts TEXT NOT NULL,
  from_state TEXT, to_state TEXT, note TEXT NOT NULL DEFAULT '', data_json TEXT);
-- the idempotency guard: one row per (incident, action), ever
CREATE TABLE IF NOT EXISTS executions (
  incident_id TEXT NOT NULL, action TEXT NOT NULL, initiator TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL,
  PRIMARY KEY (incident_id, action));
"""
_FIELDS = {"diagnosis_json", "policy_json", "approval"}


class StaleState(Exception):
    """The incident moved under us (another worker got there first)."""


class Store:
    def __init__(self, path: str = ":memory:", clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def _now(self) -> str:
        return self._clock().isoformat()

    # --- incidents -------------------------------------------------------
    def create_incident(self, pod_uid, namespace, pod, selector, kind, detail) -> str | None:
        """Returns the new id, or None if this pod already has an open incident."""
        iid, now = uuid.uuid4().hex[:8], self._now()
        with self._lock:
            try:
                self._db.execute("INSERT INTO incidents(id,pod_uid,namespace,pod,selector,finding_kind,"
                                 "finding_detail,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (iid, pod_uid, namespace, pod, selector, kind, detail, State.DETECTED.value, now, now))
            except sqlite3.IntegrityError:
                return None
            self._db.execute("INSERT INTO events(incident_id,ts,to_state,note) VALUES(?,?,?,?)",
                             (iid, now, State.DETECTED.value, detail))
        return iid

    def get(self, iid: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        if row is None:
            raise KeyError(iid)
        return dict(row)

    def list_incidents(self, state: State | None = None) -> list[dict]:
        q, args = "SELECT * FROM incidents", ()
        if state:
            q, args = q + " WHERE state=?", (state.value,)
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY created_at", args)]

    def transition(self, iid: str, to: State, note: str = "", **fields) -> None:
        """Compare-and-swap on the current state; illegal moves raise, never apply."""
        bad = set(fields) - _FIELDS
        if bad:
            raise ValueError(f"unknown fields {bad}")
        with self._lock:
            cur = State(self.get(iid)["state"])
            if not can_transition(cur, to):
                raise IllegalTransition(f"{cur.value} -> {to.value}")
            sets = ", ".join(["state=?", "updated_at=?"] + [f"{k}=?" for k in fields])
            now = self._now()
            n = self._db.execute(f"UPDATE incidents SET {sets} WHERE id=? AND state=?",
                                 (to.value, now, *fields.values(), iid, cur.value)).rowcount
            if n != 1:
                raise StaleState(iid)
            self._db.execute("INSERT INTO events(incident_id,ts,from_state,to_state,note,data_json) VALUES(?,?,?,?,?,?)",
                             (iid, now, cur.value, to.value, note, json.dumps(fields) if fields else None))

    def add_note(self, iid: str, note: str, data: dict | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events(incident_id,ts,note,data_json) VALUES(?,?,?,?)",
                             (iid, self._now(), note, json.dumps(data) if data else None))

    def events(self, iid: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM events WHERE incident_id=? ORDER BY seq", (iid,))]

    # --- approvals -------------------------------------------------------
    def decide_approval(self, iid: str, approve: bool) -> bool:
        """Human decision. Only valid while the incident awaits one."""
        with self._lock:
            n = self._db.execute("UPDATE incidents SET approval=?, updated_at=? WHERE id=? AND state=? AND approval='PENDING'",
                                 ("APPROVED" if approve else "REJECTED", self._now(), iid, State.ACTION_PROPOSED.value)).rowcount
        if n:
            self.add_note(iid, "human " + ("approved" if approve else "rejected") + " the proposed action")
        return n == 1

    def decided_approvals(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM incidents WHERE state=? AND approval IN ('APPROVED','REJECTED')", (State.ACTION_PROPOSED.value,))]

    # --- execution claims (idempotency) ---------------------------------
    def claim_execution(self, iid: str, action: str, initiator: str) -> bool:
        """Atomically claim the right to run `action` for this incident. False = already claimed.
        Claim-before-act gives at-most-once: a crash after the claim leaves the incident for a human
        rather than risking a repeat."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO executions(incident_id,action,initiator,status,ts) VALUES(?,?,?,?,?)",
                                 (iid, action, initiator, "CLAIMED", self._now()))
                return True
            except sqlite3.IntegrityError:
                return False

    def finish_execution(self, iid: str, action: str, status: str, detail: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE executions SET status=?, detail=? WHERE incident_id=? AND action=?",
                             (status, detail, iid, action))

    def attempted_actions(self, iid: str) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT action FROM executions WHERE incident_id=?", (iid,))}

    def autonomous_count_since(self, seconds: float) -> int:
        cutoff = (self._clock() - timedelta(seconds=seconds)).isoformat()
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM executions WHERE initiator='autonomous' AND ts>=?",
                                    (cutoff,)).fetchone()[0]

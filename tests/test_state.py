import itertools
import threading

import pytest

from agent.state.machine import TERMINAL, TRANSITIONS, IllegalTransition, State, can_transition
from agent.state.store import StaleState, Store

S = State
LEGAL = {
    (S.DETECTED, S.DIAGNOSING), (S.DIAGNOSING, S.ACTION_PROPOSED), (S.ACTION_PROPOSED, S.REMEDIATING),
    (S.REMEDIATING, S.VERIFYING), (S.REMEDIATING, S.REMEDIATION_FAILED),
    (S.VERIFYING, S.RESOLVED), (S.VERIFYING, S.REMEDIATION_FAILED),
}


def test_exact_transition_table():
    """Every pair is checked: only the listed forward moves + escalate-from-any-open-state are legal."""
    for a, b in itertools.product(State, State):
        expected = (a, b) in LEGAL or (b is S.ESCALATED and a not in TERMINAL)
        assert can_transition(a, b) is expected, (a, b)


def test_terminal_states_have_no_exits():
    for t in TERMINAL:
        assert TRANSITIONS[t] == frozenset()


def test_cannot_skip_verification_or_policy():
    assert not can_transition(S.REMEDIATING, S.RESOLVED)
    assert not can_transition(S.DIAGNOSING, S.REMEDIATING)
    assert not can_transition(S.DETECTED, S.REMEDIATING)


def new(store=None):
    store = store or Store()
    return store, store.create_incident("uid1", "demo", "pod1", "app=x", "CRASH_LOOP", "detail")


def test_store_walks_happy_path_and_records_timeline():
    st, i = new()
    for s in (S.DIAGNOSING, S.ACTION_PROPOSED, S.REMEDIATING, S.VERIFYING, S.RESOLVED):
        st.transition(i, s, f"to {s.value}")
    assert st.get(i)["state"] == "RESOLVED"
    assert [e["to_state"] for e in st.events(i)] == ["DETECTED", "DIAGNOSING", "ACTION_PROPOSED",
                                                     "REMEDIATING", "VERIFYING", "RESOLVED"]


def test_store_rejects_illegal_transition_and_leaves_state_untouched():
    st, i = new()
    with pytest.raises(IllegalTransition):
        st.transition(i, S.REMEDIATING)
    assert st.get(i)["state"] == "DETECTED" and len(st.events(i)) == 1


def test_terminal_incident_is_frozen():
    st, i = new()
    st.transition(i, S.ESCALATED)
    with pytest.raises(IllegalTransition):
        st.transition(i, S.DIAGNOSING)


def test_one_open_incident_per_pod_but_new_after_terminal():
    st, i = new()
    assert st.create_incident("uid1", "demo", "pod1", "app=x", "OOM_KILLED", "d") is None
    st.transition(i, S.ESCALATED)
    assert st.create_incident("uid1", "demo", "pod1", "app=x", "OOM_KILLED", "d") is not None


def test_unknown_fields_rejected():
    st, i = new()
    with pytest.raises(ValueError):
        st.transition(i, S.DIAGNOSING, state="RESOLVED")


def test_persists_across_connections(tmp_path):
    path = str(tmp_path / "t.db")
    st, i = new(Store(path))
    st.transition(i, S.DIAGNOSING)
    assert Store(path).get(i)["state"] == "DIAGNOSING"


def test_concurrent_transition_exactly_one_winner():
    st, i = new()
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            st.transition(i, S.DIAGNOSING)
            results.append("ok")
        except (IllegalTransition, StaleState):
            results.append("lost")

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count("ok") == 1


def test_execution_claim_is_exactly_once():
    st, i = new()
    assert st.claim_execution(i, "RESTART_POD", "autonomous") is True
    assert st.claim_execution(i, "RESTART_POD", "human") is False
    assert st.claim_execution(i, "ROLLBACK", "human") is True
    assert st.attempted_actions(i) == {"RESTART_POD", "ROLLBACK"}


def test_concurrent_claims_single_winner():
    st, i = new()
    wins, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        wins.append(st.claim_execution(i, "RESTART_POD", "autonomous"))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert wins.count(True) == 1


def test_approval_only_valid_while_pending():
    st, i = new()
    assert st.decide_approval(i, True) is False  # not awaiting a decision
    st.transition(i, S.DIAGNOSING)
    st.transition(i, S.ACTION_PROPOSED, approval="PENDING")
    assert st.decide_approval(i, True) is True
    assert st.decide_approval(i, False) is False  # can't flip a decision
    assert [x["id"] for x in st.decided_approvals()] == [i]


def test_autonomous_count_window():
    from datetime import datetime, timedelta, timezone
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    st = Store(clock=lambda: now[0])
    i = st.create_incident("u", "d", "p", "s", "k", "d")
    st.claim_execution(i, "RESTART_POD", "autonomous")
    st.claim_execution(i, "ROLLBACK", "human")  # human actions don't count
    assert st.autonomous_count_since(3600) == 1
    now[0] += timedelta(hours=2)
    assert st.autonomous_count_since(3600) == 0

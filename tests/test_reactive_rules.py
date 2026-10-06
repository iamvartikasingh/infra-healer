import pytest

from agent.detect.reactive_rules import FindingKind, Severity, evaluate
from tests.helpers import snap


def kinds(s, **kw):
    return {f.kind for f in evaluate(s, **kw)}


def test_healthy_pod_has_no_findings():
    assert evaluate(snap()) == []


def test_crash_loop():
    fs = evaluate(snap(waiting_reason="CrashLoopBackOff", restart_count=2, last_terminated_exit_code=1))
    assert [f.kind for f in fs] == [FindingKind.CRASH_LOOP]
    assert fs[0].severity is Severity.CRITICAL


def test_other_waiting_reasons_are_not_crash_loop():
    assert evaluate(snap(waiting_reason="ContainerCreating")) == []


def test_oom_killed_from_last_state_even_when_running():
    assert kinds(snap(last_terminated_reason="OOMKilled", restart_count=1)) == {FindingKind.OOM_KILLED}


def test_plain_error_termination_is_not_oom():
    assert evaluate(snap(last_terminated_reason="Error", restart_count=1)) == []


def test_oom_dedupe_key_changes_per_kill():
    a = evaluate(snap(last_terminated_reason="OOMKilled", restart_count=1))[0]
    b = evaluate(snap(last_terminated_reason="OOMKilled", restart_count=2))[0]
    assert a.dedupe_key != b.dedupe_key


@pytest.mark.parametrize("n,expected", [(2, False), (3, True), (10, True)])
def test_restart_threshold(n, expected):
    assert (FindingKind.RESTART_THRESHOLD in kinds(snap(restart_count=n))) is expected


def test_restart_threshold_is_configurable():
    assert FindingKind.RESTART_THRESHOLD in kinds(snap(restart_count=1), restart_threshold=1)


def test_multiple_rules_can_fire_together():
    s = snap(waiting_reason="CrashLoopBackOff", restart_count=5)
    assert kinds(s) == {FindingKind.CRASH_LOOP, FindingKind.RESTART_THRESHOLD}


def test_dedupe_key_is_per_pod():
    a = evaluate(snap(uid="a", waiting_reason="CrashLoopBackOff"))[0]
    b = evaluate(snap(uid="b", waiting_reason="CrashLoopBackOff"))[0]
    assert a.dedupe_key != b.dedupe_key

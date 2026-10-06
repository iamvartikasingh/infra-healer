"""The policy engine is the trust boundary, so it is tested three ways:
named scenarios, config validation, and an EXHAUSTIVE sweep of the decision
space asserting the safety invariants hold for every single input."""
import itertools
import json
import math

import pytest

from agent.diagnose.schema import Action, Diagnosis
from agent.policy.engine import EXECUTABLE, Mode, Outcome, PolicyConfig, PolicyContext, evaluate


def dx(action=Action.RESTART_POD, conf=0.95):
    return Diagnosis.model_validate_json(json.dumps({"rootCause": "x", "confidence": conf,
                                                     "recommendedAction": action.value, "reasoning": "y"}))


def cfg(mode=Mode.AUTONOMOUS, **kw):
    return PolicyConfig(mode=mode, **kw)


# ---------------- named scenarios ----------------
def test_autonomous_confident_whitelisted_executes():
    d = evaluate(dx(conf=0.9), cfg())
    assert d.outcome is Outcome.EXECUTE and d.rule == "AUTONOMOUS_OK"


def test_threshold_is_inclusive():
    assert evaluate(dx(conf=0.8), cfg()).outcome is Outcome.EXECUTE
    assert evaluate(dx(conf=0.7999), cfg()).outcome is Outcome.REQUIRE_APPROVAL


def test_low_confidence_asks_a_human_rather_than_acting_or_dropping():
    d = evaluate(dx(conf=0.3), cfg())
    assert (d.outcome, d.rule) == (Outcome.REQUIRE_APPROVAL, "LOW_CONFIDENCE")


def test_human_approval_mode_always_asks_even_at_full_confidence():
    d = evaluate(dx(conf=1.0), cfg(Mode.HUMAN_APPROVAL))
    assert (d.outcome, d.rule) == (Outcome.REQUIRE_APPROVAL, "APPROVAL_REQUIRED")


def test_observe_only_never_acts_even_if_human_approved():
    for approved in (False, True):
        d = evaluate(dx(conf=1.0), cfg(Mode.OBSERVE_ONLY), PolicyContext(human_approved=approved))
        assert (d.outcome, d.rule) == (Outcome.DENY, "OBSERVE_ONLY")


def test_whitelist_beats_confidence_and_human_approval():
    c = cfg(allowed_actions=frozenset({Action.RESTART_POD}))
    for approved in (False, True):
        d = evaluate(dx(Action.ROLLBACK, 1.0), c, PolicyContext(human_approved=approved))
        assert (d.outcome, d.rule) == (Outcome.DENY, "NOT_WHITELISTED")


def test_default_whitelist_is_restart_only():
    assert PolicyConfig().allowed_actions == {Action.RESTART_POD}
    assert evaluate(dx(Action.SCALE_UP), cfg()).rule == "NOT_WHITELISTED"


@pytest.mark.parametrize("a", [Action.ESCALATE_TO_HUMAN, Action.NO_ACTION])
def test_advisory_actions_are_never_executable(a):
    assert evaluate(dx(a, 1.0), cfg()).rule == "NOT_EXECUTABLE"


def test_already_attempted_is_denied_even_with_approval():
    ctx = PolicyContext(attempted_actions=frozenset({Action.RESTART_POD}), human_approved=True)
    assert evaluate(dx(), cfg(), ctx).rule == "ALREADY_ATTEMPTED"


def test_a_different_action_is_not_blocked_by_an_earlier_attempt():
    c = cfg(allowed_actions=frozenset({Action.RESTART_POD, Action.ROLLBACK}))
    ctx = PolicyContext(attempted_actions=frozenset({Action.RESTART_POD}))
    assert evaluate(dx(Action.ROLLBACK), c, ctx).outcome is Outcome.EXECUTE


def test_human_approval_executes_regardless_of_low_confidence():
    d = evaluate(dx(conf=0.1), cfg(Mode.HUMAN_APPROVAL), PolicyContext(human_approved=True))
    assert (d.outcome, d.rule) == (Outcome.EXECUTE, "HUMAN_APPROVED")


def test_rate_limit_degrades_autonomy_to_approval():
    c = cfg(max_autonomous_per_window=2)
    assert evaluate(dx(), c, PolicyContext(autonomous_in_window=1)).outcome is Outcome.EXECUTE
    assert evaluate(dx(), c, PolicyContext(autonomous_in_window=2)).rule == "RATE_LIMIT"


def test_rate_limit_zero_disables_autonomy():
    assert evaluate(dx(), cfg(max_autonomous_per_window=0)).outcome is Outcome.REQUIRE_APPROVAL


def test_human_approval_is_not_rate_limited():
    d = evaluate(dx(), cfg(max_autonomous_per_window=0), PolicyContext(autonomous_in_window=99, human_approved=True))
    assert d.outcome is Outcome.EXECUTE


def test_no_diagnosis_denies():
    assert evaluate(None, cfg()).rule == "NO_DIAGNOSIS"


def test_decision_is_explainable_and_serialisable():
    j = evaluate(dx(conf=0.9), cfg()).to_json()
    assert j == {"outcome": "EXECUTE", "rule": "AUTONOMOUS_OK", "reason": j["reason"],
                 "action": "RESTART_POD", "confidence": 0.9}


# --------- the engine re-validates; it does not trust the model object ---------
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -0.1, 1.5, None, "0.9", True])
def test_corrupt_confidence_is_denied(bad):
    d = Diagnosis.model_construct(root_cause="x", confidence=bad, recommended_action=Action.RESTART_POD, reasoning="y")
    out = evaluate(d, cfg(), PolicyContext(human_approved=True))
    assert out.outcome is Outcome.DENY and out.rule == "INVALID_CONFIDENCE"


@pytest.mark.parametrize("bad", ["RESTART_POD", "rm -rf /", None, 42])
def test_non_enum_action_is_denied(bad):
    d = Diagnosis.model_construct(root_cause="x", confidence=1.0, recommended_action=bad, reasoning="y")
    out = evaluate(d, cfg(), PolicyContext(human_approved=True))
    assert out.outcome is Outcome.DENY and out.rule == "INVALID_ACTION"


# ---------------- config validation: misconfig must fail loudly at startup -------------
@pytest.mark.parametrize("t", [0, -0.1, 1.01, math.nan, math.inf, None, "0.8", True])
def test_bad_threshold_rejected(t):
    with pytest.raises(ValueError):
        PolicyConfig(confidence_threshold=t)


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        PolicyConfig(mode="AUTONOMOUS")  # a raw string must not silently pass


@pytest.mark.parametrize("bad", [{Action.ESCALATE_TO_HUMAN}, {Action.NO_ACTION}, {"RESTART_POD"}])
def test_whitelist_may_only_hold_executable_actions(bad):
    with pytest.raises(ValueError):
        PolicyConfig(allowed_actions=frozenset(bad))


@pytest.mark.parametrize("n", [-1, 1.5, "3"])
def test_bad_rate_limit_rejected(n):
    with pytest.raises(ValueError):
        PolicyConfig(max_autonomous_per_window=n)


def test_threshold_of_exactly_one_is_allowed():
    assert evaluate(dx(conf=1.0), cfg(confidence_threshold=1.0)).outcome is Outcome.EXECUTE


# ---------------- exhaustive invariant sweep ----------------
ACTIONS = list(Action)
CONFS = [0.0, 0.1, 0.5, 0.79, 0.8, 0.81, 0.99, 1.0]
WHITELISTS = [frozenset(), frozenset({Action.RESTART_POD}), frozenset({Action.SCALE_UP, Action.ROLLBACK}), EXECUTABLE]
ATTEMPTED = [frozenset(), frozenset({Action.RESTART_POD}), frozenset(EXECUTABLE)]


def test_exhaustive_safety_invariants():
    n = 0
    for mode, action, conf, wl, attempted, in_window, approved in itertools.product(
            Mode, ACTIONS, CONFS, WHITELISTS, ATTEMPTED, [0, 2, 3, 10], [False, True]):
        c = PolicyConfig(mode=mode, confidence_threshold=0.8, allowed_actions=wl, max_autonomous_per_window=3)
        ctx = PolicyContext(attempted_actions=attempted, autonomous_in_window=in_window, human_approved=approved)
        d = evaluate(dx(action, conf), c, ctx)
        n += 1
        if d.outcome is Outcome.EXECUTE:
            where = (mode, action, conf, wl, attempted, in_window, approved)
            assert action in EXECUTABLE, where           # advisory actions never run
            assert action in wl, where                   # whitelist is absolute
            assert mode is not Mode.OBSERVE_ONLY, where  # observe-only never runs anything
            assert action not in attempted, where        # never repeat an action for an incident
            # and it got there only via an explicit road:
            assert approved or (mode is Mode.AUTONOMOUS and conf >= 0.8 and in_window < 3), where
        if d.outcome is Outcome.REQUIRE_APPROVAL:
            assert action in EXECUTABLE and action in wl and mode is not Mode.OBSERVE_ONLY
            assert not approved
        assert d.rule and d.reason  # every decision explains itself
    assert n == 3 * 5 * 8 * 4 * 3 * 4 * 2


def test_evaluate_is_deterministic_and_does_not_mutate_inputs():
    c, ctx, d = cfg(), PolicyContext(attempted_actions=frozenset({Action.SCALE_UP})), dx()
    first = evaluate(d, c, ctx)
    assert all(evaluate(d, c, ctx) == first for _ in range(50))
    assert d.confidence == 0.95 and ctx.attempted_actions == {Action.SCALE_UP}

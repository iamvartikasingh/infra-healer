"""The trust boundary: the only thing between an AI suggestion and a cluster change.

`evaluate` is a pure function of (diagnosis, config, context). It performs no I/O,
reads no clock and holds no state, so every decision is reproducible and the whole
decision space can be tested exhaustively.

Design rules
  * Fail closed. Anything unexpected -> DENY. The default outcome is "don't act".
  * Ordered rules, first match wins, each named so a decision is explainable.
  * The whitelist is a hard bound in EVERY mode, including after a human approves.
  * The model's output is untrusted even after schema validation: type, range and
    enum membership are re-checked here, independently of the validator.
  * EXECUTE is reachable only through the two explicit paths at the bottom.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from agent.diagnose.schema import Action, Diagnosis

EXECUTABLE = frozenset({Action.RESTART_POD, Action.SCALE_UP, Action.ROLLBACK})


class Mode(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"      # never acts; records what it would have done
    HUMAN_APPROVAL = "HUMAN_APPROVAL"  # proposes; a human must approve every action
    AUTONOMOUS = "AUTONOMOUS"          # acts alone only when confident enough


class Outcome(str, Enum):
    EXECUTE = "EXECUTE"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"  # -> incident escalates to a human


@dataclass(frozen=True)
class PolicyConfig:
    mode: Mode = Mode.HUMAN_APPROVAL  # default is the cautious middle, not autonomy
    confidence_threshold: float = 0.8
    allowed_actions: frozenset = frozenset({Action.RESTART_POD})  # least privilege by default
    max_autonomous_per_window: int = 3  # blast-radius circuit breaker
    window_seconds: float = 3600.0
    # Acting on a pod that has not failed yet is riskier than reacting to one that has.
    allow_autonomous_predictive: bool = False

    def __post_init__(self):
        # Misconfiguration must fail at startup, never silently loosen the boundary.
        if not isinstance(self.mode, Mode):
            raise ValueError(f"mode must be a Mode, got {self.mode!r}")
        t = self.confidence_threshold
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 < t <= 1:
            raise ValueError(f"confidence_threshold must be in (0, 1], got {t!r}")
        allowed = frozenset(self.allowed_actions)
        if not all(isinstance(a, Action) for a in allowed):  # a str enum equals its raw string; don't accept those
            raise ValueError("allowed_actions must contain Action members, not raw strings")
        if not allowed <= EXECUTABLE:
            raise ValueError(f"allowed_actions may only contain executable actions, got {sorted(map(str, allowed - EXECUTABLE))}")
        object.__setattr__(self, "allowed_actions", allowed)
        if not isinstance(self.allow_autonomous_predictive, bool):
            raise ValueError("allow_autonomous_predictive must be a bool")
        if not isinstance(self.max_autonomous_per_window, int) or self.max_autonomous_per_window < 0:
            raise ValueError("max_autonomous_per_window must be an int >= 0")


@dataclass(frozen=True)
class PolicyContext:
    """Facts about the world the caller knows and the engine must not guess."""
    attempted_actions: frozenset = frozenset()  # actions already claimed for this incident
    autonomous_in_window: int = 0               # autonomous executions in the rate window
    human_approved: bool = False                # a human approved THIS proposal
    predictive: bool = False                    # incident came from a prediction, not an observed failure


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    rule: str
    reason: str
    action: Action | None = None
    confidence: float | None = None

    def to_json(self) -> dict:
        return {"outcome": self.outcome.value, "rule": self.rule, "reason": self.reason,
                "action": self.action.value if self.action else None, "confidence": self.confidence}


def evaluate(diagnosis: Diagnosis | None, config: PolicyConfig,
             ctx: PolicyContext = PolicyContext()) -> PolicyDecision:
    def deny(rule: str, why: str, a=None, c=None) -> PolicyDecision:
        return PolicyDecision(Outcome.DENY, rule, why, a, c)

    if diagnosis is None:
        return deny("NO_DIAGNOSIS", "no valid diagnosis available")

    action, conf = getattr(diagnosis, "recommended_action", None), getattr(diagnosis, "confidence", None)
    # Re-validate independently of the schema layer (defense in depth).
    if not isinstance(action, Action):
        return deny("INVALID_ACTION", "recommended action is not a known Action")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        return deny("INVALID_CONFIDENCE", "confidence missing, non-finite or outside [0, 1]", action)

    def decide(outcome: Outcome, rule: str, why: str) -> PolicyDecision:
        return PolicyDecision(outcome, rule, why, action, float(conf))

    if action not in EXECUTABLE:
        return decide(Outcome.DENY, "NOT_EXECUTABLE", f"{action.value} is advisory; a human should look")
    if action not in config.allowed_actions:
        return decide(Outcome.DENY, "NOT_WHITELISTED", f"{action.value} is not in the allowed action whitelist")
    if config.mode is Mode.OBSERVE_ONLY:
        return decide(Outcome.DENY, "OBSERVE_ONLY", f"observe-only mode; would have proposed {action.value}")
    if action in ctx.attempted_actions:
        return decide(Outcome.DENY, "ALREADY_ATTEMPTED", f"{action.value} was already attempted for this incident")

    # --- the only two roads to EXECUTE -----------------------------------
    if ctx.human_approved:
        return decide(Outcome.EXECUTE, "HUMAN_APPROVED", "a human approved this action")
    if config.mode is Mode.HUMAN_APPROVAL:
        return decide(Outcome.REQUIRE_APPROVAL, "APPROVAL_REQUIRED", "human-approval mode: every action needs sign-off")
    if config.mode is Mode.AUTONOMOUS:
        if ctx.predictive and not config.allow_autonomous_predictive:
            return decide(Outcome.REQUIRE_APPROVAL, "PREDICTIVE_NEEDS_HUMAN",
                          "predicted (not yet observed) failure: a human must approve acting early")
        if conf < config.confidence_threshold:
            return decide(Outcome.REQUIRE_APPROVAL, "LOW_CONFIDENCE",
                          f"confidence {conf:.2f} below threshold {config.confidence_threshold:.2f}")
        if ctx.autonomous_in_window >= config.max_autonomous_per_window:
            return decide(Outcome.REQUIRE_APPROVAL, "RATE_LIMIT",
                          f"{ctx.autonomous_in_window} autonomous actions in window (max {config.max_autonomous_per_window})")
        return decide(Outcome.EXECUTE, "AUTONOMOUS_OK", f"confidence {conf:.2f} >= {config.confidence_threshold:.2f}, whitelisted")
    return decide(Outcome.DENY, "UNKNOWN_MODE", "unrecognised mode")  # unreachable; fail closed anyway

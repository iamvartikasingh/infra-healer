"""Incident lifecycle. The legal transitions are data, enforced by the store."""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    DETECTED = "DETECTED"
    DIAGNOSING = "DIAGNOSING"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    REMEDIATION_FAILED = "REMEDIATION_FAILED"
    ESCALATED = "ESCALATED"


TERMINAL = frozenset({State.RESOLVED, State.ESCALATED})

_FORWARD = {
    State.DETECTED: {State.DIAGNOSING},
    State.DIAGNOSING: {State.ACTION_PROPOSED},
    State.ACTION_PROPOSED: {State.REMEDIATING},
    State.REMEDIATING: {State.VERIFYING, State.REMEDIATION_FAILED},
    State.VERIFYING: {State.RESOLVED, State.REMEDIATION_FAILED},
    State.REMEDIATION_FAILED: set(),
    State.RESOLVED: set(),
    State.ESCALATED: set(),
}
# Any non-terminal state may escalate to a human: the universal safe exit
# (failed diagnosis, policy denial, rejected approval, unexpected error).
TRANSITIONS: dict[State, frozenset[State]] = {
    s: frozenset(nxt | ({State.ESCALATED} if s not in TERMINAL else set()))
    for s, nxt in _FORWARD.items()
}


class IllegalTransition(Exception):
    pass


def can_transition(src: State, dst: State) -> bool:
    return dst in TRANSITIONS[src]

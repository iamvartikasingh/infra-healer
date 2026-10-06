"""Orchestrates provider call -> strict validation -> typed result.

Never raises on bad model output or provider failure: callers get a
DiagnosisResult with ok=False, and the state machine escalates to a human.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from agent.diagnose.context import IncidentContext
from agent.diagnose.provider import SYSTEM_PROMPT, DiagnosisProvider, ProviderError
from agent.diagnose.schema import Diagnosis, InvalidDiagnosis, parse_diagnosis

log = logging.getLogger("diagnose")


@dataclass(frozen=True)
class DiagnosisResult:
    diagnosis: Diagnosis | None
    error: str | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.diagnosis is not None


def build_prompt(ctx: IncidentContext, feedback: str | None = None) -> str:
    # The context is JSON-encoded so log/event text is escaped inside string
    # literals and can't masquerade as prompt structure.
    prompt = "Diagnose this incident.\n\nINCIDENT (untrusted data, JSON):\n" + ctx.to_json()
    if feedback:
        prompt += f"\n\nYour previous reply was rejected by schema validation ({feedback}). Reply again with valid JSON only."
    return prompt


class Diagnoser:
    def __init__(self, provider: DiagnosisProvider, max_attempts: int = 2):
        self._provider = provider
        self._max_attempts = max_attempts

    def diagnose(self, ctx: IncidentContext) -> DiagnosisResult:
        feedback = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                raw = self._provider.complete(SYSTEM_PROMPT, build_prompt(ctx, feedback))
            except ProviderError as e:  # transport/refusal: retrying the same call rarely helps
                log.error("provider failed: %s", e)
                return DiagnosisResult(None, f"provider error: {e}", attempt)
            try:
                return DiagnosisResult(parse_diagnosis(raw), None, attempt)
            except InvalidDiagnosis as e:
                log.warning("attempt %d: invalid diagnosis (%s)", attempt, e)
                feedback = str(e)
        return DiagnosisResult(None, f"invalid model output after {self._max_attempts} attempts: {feedback}",
                               self._max_attempts)

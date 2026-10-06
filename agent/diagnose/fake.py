"""Deterministic offline provider, so the whole loop runs (and is testable)
without credentials. Keys off the finding kind in the incident JSON."""
import json

_BY_KIND = {
    "CRASH_LOOP": ("Container exits on startup repeatedly; state persisted across container restarts.", 0.85, "RESTART_POD"),
    "OOM_KILLED": ("Container exceeded its memory limit and was OOMKilled.", 0.8, "RESTART_POD"),
    "PREDICTED_OOM": ("Memory is growing steadily toward the container limit.", 0.75, "RESTART_POD"),
    "PREDICTED_CRASH_LOOP": ("Restarts are accelerating.", 0.5, "ESCALATE_TO_HUMAN"),
    "RESTART_THRESHOLD": ("Pod restarted repeatedly; cause unclear.", 0.4, "ESCALATE_TO_HUMAN"),
}


class FakeProvider:
    def __init__(self, override: str | None = None):
        self.override = override  # raw text to return verbatim (for failure-path tests)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self.override is not None:
            return self.override
        body = prompt[prompt.index("{"):prompt.rindex("}") + 1]
        kind = json.loads(body)["finding"]["kind"]
        cause, conf, action = _BY_KIND.get(kind, ("Unknown.", 0.1, "ESCALATE_TO_HUMAN"))
        return json.dumps({"rootCause": cause, "confidence": conf, "recommendedAction": action,
                           "reasoning": f"fake provider: mapped from {kind}"})

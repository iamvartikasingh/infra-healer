"""The contract between the LLM and the rest of the system.

Nothing downstream may consume model output that hasn't passed through
`parse_diagnosis`. Validation is strict: unknown fields, wrong types, an
action outside the enum, or confidence outside [0, 1] all reject the response.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Action(str, Enum):
    # Executable (subject to the policy whitelist):
    RESTART_POD = "RESTART_POD"
    SCALE_UP = "SCALE_UP"
    ROLLBACK = "ROLLBACK"
    # Never executed; the model's way of saying "a human should look":
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    NO_ACTION = "NO_ACTION"


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    root_cause: str = Field(alias="rootCause", min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: Action = Field(alias="recommendedAction")
    reasoning: str = Field(min_length=1, max_length=2000)


class InvalidDiagnosis(ValueError):
    """Model output failed validation. Carries a short, safe-to-echo reason."""


def parse_diagnosis(raw: str) -> Diagnosis:
    try:
        return Diagnosis.model_validate_json(raw)
    except ValidationError as e:
        # Summarise by location+type only; never echo model/log text back verbatim.
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['type']}"
                             for err in e.errors())
        raise InvalidDiagnosis(problems) from e


def json_schema() -> dict:
    """JSON Schema handed to the API's structured-output constraint."""
    schema = Diagnosis.model_json_schema(by_alias=True)
    schema["additionalProperties"] = False
    return schema

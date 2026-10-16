"""The model's output contract and its validator.

Three rules, enforced in code rather than hoped for in the prompt:

1. Every claim cites an item that exists in the evidence pack; a claim whose
   citation is not in the pack is dropped. A hypothesis left with no supporting
   evidence is withdrawn and the Brief says ``insufficient_evidence``.
2. ``alternatives`` is mandatory and non-empty, so the responder evaluates
   rather than anchors. A response without it is invalid.
3. ``insufficient_evidence`` is a first-class, successful outcome.

Anything unparseable or off-schema yields a *degraded* result: the Brief ships
with evidence and links but no hypothesis. Never a partial one.
"""

from __future__ import annotations

import json
from collections.abc import Set
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

EvidenceSource = Literal["logs", "metrics", "deployments", "alert", "knowledge"]


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(min_length=1, max_length=600)
    source: EvidenceSource
    citation: str = Field(min_length=1, max_length=300)  # a key from the evidence pack


class Alternative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=1, max_length=400)
    why_less_likely: str = Field(min_length=1, max_length=400)


class NextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int = Field(ge=1, le=10)
    action: str = Field(min_length=1, max_length=400)
    expected_signal: str = Field(min_length=1, max_length=400)
    risk: str = Field(min_length=1, max_length=300)


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str | None = Field(default=None, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceClaim] = Field(default_factory=list, max_length=12)
    alternatives: list[Alternative] = Field(default_factory=list, max_length=5)
    next_steps: list[NextStep] = Field(default_factory=list, max_length=6)
    insufficient_evidence: bool = False
    missing: list[str] = Field(default_factory=list, max_length=8)  # what would have helped


def response_schema() -> dict[str, Any]:
    """JSON schema handed to the model as its structured-output contract."""
    return ModelOutput.model_json_schema()


class Outcome(StrEnum):
    COMPLETE = "complete"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class ValidationResult:
    outcome: Outcome
    output: ModelOutput | None
    dropped_claims: int = 0
    reason: str | None = None


def validate(raw: str | dict[str, Any], pack_keys: Set[str]) -> ValidationResult:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        output = ModelOutput.model_validate(data)
    except (json.JSONDecodeError, TypeError) as exc:
        return ValidationResult(Outcome.DEGRADED, None, reason=f"unparseable: {exc}")
    except ValidationError as exc:
        return ValidationResult(Outcome.DEGRADED, None, reason=f"schema: {exc.errors()[0]['msg']}")

    kept = [e for e in output.evidence if e.citation in pack_keys]
    dropped = len(output.evidence) - len(kept)

    if output.insufficient_evidence or output.hypothesis is None or not kept:
        withdrawn = output.model_copy(
            update={"hypothesis": None, "evidence": kept, "insufficient_evidence": True, "confidence": 0.0}
        )
        reason = None if output.insufficient_evidence else "no claim survived citation checks"
        return ValidationResult(Outcome.INSUFFICIENT_EVIDENCE, withdrawn, dropped, reason)

    if not output.alternatives:
        return ValidationResult(Outcome.DEGRADED, None, dropped, reason="alternatives missing")

    steps = sorted(output.next_steps, key=lambda s: s.priority)
    return ValidationResult(
        Outcome.COMPLETE, output.model_copy(update={"evidence": kept, "next_steps": steps}), dropped
    )

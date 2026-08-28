"""Deterministic answer grading. No model self-scoring: every input here is a
number or boolean computed elsewhere (compiler, diagnose, sanitize, gold
question fixtures); grade() only applies fixed thresholds to them.

A Refusal (from core.sanitize, surfaced through core.extract) or a
TenantScopeError (from core.compile, wrapped by the caller into a Refusal)
forces REFUSE directly. It is never re-derived from the heuristic checks
below: a guardrail refusal is carried, not re-decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

COMPLETENESS_MIN_PCT = 99.0
REMAINDER_MAX_PCT = 15.0


class Grade(str, Enum):
    CERTIFIED = "CERTIFIED"
    QUALIFIED = "QUALIFIED"
    CLARIFY = "CLARIFY"
    REFUSE = "REFUSE"


@dataclass(frozen=True)
class Refusal:
    reason: str
    source: str  # "sanitize" | "tenant" | ...


@dataclass(frozen=True)
class GradeInputs:
    refusal: Refusal | None = None
    clarify: str | None = None
    registry_covered: bool = True
    freshness_hours: float | None = None
    freshness_sla_hours: float | None = None
    completeness_pct: float | None = None
    window_closed_to_corrections: bool = True
    gold_match: bool | None = None
    unexplained_remainder_pct: float | None = None


@dataclass(frozen=True)
class GradeResult:
    grade: Grade
    reasons: list[str]


def grade(inputs: GradeInputs) -> GradeResult:
    if inputs.refusal is not None:
        return GradeResult(Grade.REFUSE, [f"refused by {inputs.refusal.source}: {inputs.refusal.reason}"])

    if inputs.clarify is not None:
        return GradeResult(Grade.CLARIFY, [f"underspecified: {inputs.clarify}"])

    reasons: list[str] = []

    if not inputs.registry_covered:
        reasons.append("metric or dimension is not covered by this tenant's registry")

    if inputs.freshness_hours is not None and inputs.freshness_sla_hours is not None:
        if inputs.freshness_hours > inputs.freshness_sla_hours:
            reasons.append(
                f"data is {inputs.freshness_hours:.1f}h stale, SLA is {inputs.freshness_sla_hours:.1f}h"
            )

    if inputs.completeness_pct is not None and inputs.completeness_pct < COMPLETENESS_MIN_PCT:
        reasons.append(f"completeness {inputs.completeness_pct:.1f}% is below {COMPLETENESS_MIN_PCT:.0f}%")

    if not inputs.window_closed_to_corrections:
        reasons.append("window is still open to corrections")

    if inputs.gold_match is False:
        reasons.append("answer does not match its gold question")

    if inputs.unexplained_remainder_pct is not None and inputs.unexplained_remainder_pct > REMAINDER_MAX_PCT:
        reasons.append(
            f"unexplained remainder {inputs.unexplained_remainder_pct:.1f}% exceeds {REMAINDER_MAX_PCT:.0f}%"
        )

    if reasons:
        return GradeResult(Grade.QUALIFIED, reasons)

    return GradeResult(Grade.CERTIFIED, ["registry coverage current", "fresh within SLA", "complete",
                                          "window closed to corrections", "no gold mismatch",
                                          "remainder within bound"])

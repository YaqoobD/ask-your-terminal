"""Turns a DiagnoseResult into prose. Contract: every number in the narration
must trace back to the evidence block built from the result; the model picks
words, it never computes a figure. `verify_numbers_in_evidence` enforces this
by extraction, not by asking a judge model, and `narrate()` refuses to return
prose that fails it.
"""

from __future__ import annotations

import re

from core.diagnose import DiagnoseResult
from core.providers import get_provider

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

SYSTEM_PROMPT = (
    "You narrate a diagnostic result for an operations audience. Use only the "
    "numbers given in the evidence block below; never compute or introduce a "
    "new number. Label every item attributed to a dimension level as a "
    "'contributor', never as 'the cause'. Always mention the unexplained "
    "remainder when one is reported. If the reality check flags an artefact, "
    "say so plainly and do not present the figure as an operational change."
)


class NarrationError(Exception):
    """The narration contains a number absent from the evidence."""


def _add_number(numbers: set[str], value) -> None:
    if value is None:
        return
    numbers.add(f"{value:.0f}")
    numbers.add(f"{value:.1f}")
    numbers.add(f"{value:.2f}")
    numbers.add(f"{abs(value):.0f}")
    numbers.add(f"{abs(value):.1f}")


def _evidence_numbers(result: DiagnoseResult) -> set[str]:
    numbers: set[str] = set()
    for value in (result.base_value, result.comparison_value, result.total_delta):
        _add_number(numbers, value)
    for dim in result.dimensions:
        _add_number(numbers, dim.total_numerator_delta)
        _add_number(numbers, dim.unexplained_remainder)
        for c in dim.contributions:
            _add_number(numbers, c.numerator_delta)
            _add_number(numbers, c.denominator_delta)
            if c.share is not None:
                _add_number(numbers, c.share * 100)
    for s in result.signals:
        _add_number(numbers, s.base_value)
        _add_number(numbers, s.comparison_value)
        _add_number(numbers, s.delta)
    return numbers


def _evidence_block(result: DiagnoseResult) -> str:
    lines = [
        f"metric: {result.metric}",
        f"base_value: {result.base_value}",
        f"comparison_value: {result.comparison_value}",
        f"total_delta: {result.total_delta}",
        f"reality_check: is_artefact={result.reality.is_artefact}, reason={result.reality.reason}",
    ]
    for dim in result.dimensions:
        lines.append(
            f"dimension {dim.dimension}: total_delta={dim.total_numerator_delta}, "
            f"unexplained_remainder={dim.unexplained_remainder}"
        )
        for c in dim.contributions:
            lines.append(
                f"  contributor {c.level}: numerator_delta={c.numerator_delta}, "
                f"denominator_delta={c.denominator_delta}"
            )
    for s in result.signals:
        lines.append(f"declared signal {s.signal}: base={s.base_value}, comparison={s.comparison_value}, delta={s.delta}")
    return "\n".join(lines)


def verify_numbers_in_evidence(narration: str, result: DiagnoseResult) -> None:
    evidence = _evidence_numbers(result)
    for token in _NUMBER_RE.findall(narration):
        stripped = token.lstrip("-")
        if token in evidence or stripped in evidence:
            continue
        if "." not in stripped and float(stripped) < 10:
            continue  # small counts/ordinals ("top 3") aren't evidence figures
        raise NarrationError(f"narration contains number '{token}' not present in the evidence")


def narrate(result: DiagnoseResult, *, provider=None) -> str:
    provider = provider or get_provider("narrate")
    text = provider.complete(system=SYSTEM_PROMPT, user=_evidence_block(result))
    verify_numbers_in_evidence(text, result)
    return text

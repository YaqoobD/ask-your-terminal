"""Question + resolved registry -> QueryIntent, or a single clarifying
question. Pipeline is screen -> extract -> validate. A "block" verdict from
core.sanitize short-circuits before any provider call: extract() never talks
to a model for a blocked question.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from core.intent import QueryIntent
from core.providers import Provider, get_provider
from core.sanitize import screen

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "intent_v1.md"
PROMPT_VERSION = "intent_v1"


class ExtractError(Exception):
    """The provider returned something that isn't a valid clarify-or-intent shape."""


@dataclass(frozen=True)
class Refusal:
    reason: str
    source: str  # "sanitize" | "tenant" | ...


@dataclass(frozen=True)
class ExtractResult:
    intent: QueryIntent | None = None
    clarify: str | None = None
    refusal: Refusal | None = None


_JSON_BLOB = re.compile(r"\{.*\}", re.S)


def _metrics_block(registry) -> str:
    lines = []
    for name, metric in registry.metrics.items():
        lines.append(
            f"- {name}: {metric['definition']} "
            f"(grains: {', '.join(metric['allowed_grains'])}; "
            f"dimensions: {', '.join(metric['allowed_dimensions'])})"
        )
    return "\n".join(lines)


def _dimension_names(registry) -> str:
    names = set()
    for metric in registry.metrics.values():
        names.update(metric["allowed_dimensions"])
    return ", ".join(sorted(names))


def build_prompt(question: str, registry) -> str:
    template = _PROMPT_PATH.read_text()
    return template.format(
        metrics_block=_metrics_block(registry),
        dimension_names=_dimension_names(registry),
        question=question,
    )


def _stringify_filters(intent_dict: dict) -> None:
    """QueryIntent.filters is dict[str, str], but a boolean dimension like
    is_reefer naturally comes back from the model as a JSON bool. Normalise
    at the extraction boundary rather than trusting every response to quote it.
    """
    filters = intent_dict.get("filters")
    if not isinstance(filters, dict):
        return
    for key, value in filters.items():
        if isinstance(value, bool):
            filters[key] = "true" if value else "false"
        elif not isinstance(value, str):
            filters[key] = str(value)


def _parse_response(raw: str) -> dict:
    match = _JSON_BLOB.search(raw)
    if not match:
        raise ExtractError(f"provider response contained no JSON object: {raw!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ExtractError(f"provider response was not valid JSON: {raw!r}") from exc


def extract(question: str, registry, *, provider: Provider | None = None) -> ExtractResult:
    result = screen(question)
    if result.verdict == "block":
        return ExtractResult(refusal=Refusal(reason=result.reason, source="sanitize"))

    provider = provider or get_provider("intent")
    prompt = build_prompt(result.clean, registry)
    raw = provider.complete(system="", user=prompt)
    parsed = _parse_response(raw)

    if "clarify" in parsed:
        return ExtractResult(clarify=parsed["clarify"])

    if "intent" not in parsed:
        raise ExtractError(f"provider response had neither 'intent' nor 'clarify': {parsed!r}")

    _stringify_filters(parsed["intent"])
    try:
        intent = QueryIntent(**parsed["intent"])
    except ValidationError as exc:
        raise ExtractError(f"provider produced an intent that failed validation: {exc}") from exc

    return ExtractResult(intent=intent)


if __name__ == "__main__":
    import sys

    from registry.load import resolve

    question = sys.argv[1] if len(sys.argv) > 1 else "which berths had the worst crane idle time last month?"
    result = extract(question, resolve("tos_alpha"))
    if result.refusal:
        print(f"REFUSE ({result.refusal.source}): {result.refusal.reason}")
    elif result.clarify:
        print(f"CLARIFY: {result.clarify}")
    else:
        print(result.intent.model_dump_json(indent=2))

"""Three cache tiers, in-memory, process-lifetime.

T0 answer cache: keyed on (intent_hash, client, knowledge_watermark). The
watermark is the latest knowledge_time across the metric's own signal
tables for that tenant, so a correction landing in one table only bumps the
watermark, and only invalidates the T0 entries, for metrics that read that
table. A metric that doesn't touch the corrected table keeps its old
watermark and stays a cache hit.

T1 intent cache: keyed on normalised question text, so two rephrasings of
one question that resolve to the same intent cost one provider call, not two.

T2 prompt cache: keyed on client id, memoises whatever the caller builds
(the registry block in the extraction prompt) so it isn't rebuilt per question.
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable

import duckdb

from core.intent import QueryIntent
from registry.load import ResolvedRegistry

_WHITESPACE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower())


def intent_hash(intent: QueryIntent) -> str:
    return hashlib.sha256(intent.model_dump_json(exclude_none=True).encode()).hexdigest()


def compute_watermark(metric_name: str, resolved: ResolvedRegistry, con: duckdb.DuckDBPyConnection) -> str:
    metric = resolved.metrics[metric_name]
    signals = [resolved.signals[metric["numerator"]], resolved.signals[metric["denominator"]]]
    latest = None
    for signal in signals:
        if signal.get("virtual"):
            continue
        knowledge_col = signal.get("knowledge_time_column")
        if not knowledge_col:
            continue
        value = con.execute(
            f"select max({knowledge_col}) from {signal['table']} where terminal = ?", [resolved.terminal]
        ).fetchone()[0]
        if value is not None and (latest is None or value > latest):
            latest = value
    return latest.isoformat() if latest is not None else "unknown"


class AnswerCache:
    """T0."""

    def __init__(self):
        self._store: dict[tuple, object] = {}

    def get(self, intent_hash_: str, client: str, watermark: str):
        return self._store.get((intent_hash_, client, watermark))

    def set(self, intent_hash_: str, client: str, watermark: str, answer) -> None:
        self._store[(intent_hash_, client, watermark)] = answer

    def __len__(self) -> int:
        return len(self._store)


class IntentCache:
    """T1."""

    def __init__(self):
        self._store: dict[str, object] = {}

    def get(self, question: str):
        return self._store.get(normalize_question(question))

    def set(self, question: str, intent_or_clarify) -> None:
        self._store[normalize_question(question)] = intent_or_clarify


class PromptCache:
    """T2."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def get_or_build(self, client_id: str, builder: Callable[[], str]) -> str:
        if client_id not in self._store:
            self._store[client_id] = builder()
        return self._store[client_id]

"""Append-only runs.jsonl, one writer, two record shapes discriminated by
`kind`.

`kind: "answer"` is the supply side and carries everything a
`flag_incorrect_grade` needs to reconstruct the answer end to end: the
intent, the compiled SQL and params, the grade and the reasons that forced
it. A wrong grade has to be debuggable from the flag alone, or the flag
button is theatre.

`kind: "interaction"` is the demand side: what a human did with the card.
Every interaction carries `answer_id` so it joins back to its answer record.

`summarize()` derives the Question 4 utility numbers from this one file.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

INTERACTION_KINDS = {
    "export_csv", "copy_answer", "reveal_sql", "flag_incorrect_grade",
    "answered_clarify", "changed_what_i_did",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, row: dict) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def record_answer(
    path,
    *,
    answer_id: str,
    intent_hash: str,
    tenant_id: str,
    grade: str,
    grade_reasons: list[str],
    intent: dict,
    sql: str,
    params: list,
    prompt_version: str,
    sanitizer_verdict: str,
    tokens: dict,
    cost_usd: float,
    latency_ms: dict,
    cache_tier: str | None,
    asker_id: str | None = None,
    via_clarify: bool = False,
) -> dict:
    row = {
        "kind": "answer",
        "ts": now_iso(),
        "answer_id": answer_id,
        "intent_hash": intent_hash,
        "tenant_id": tenant_id,
        "grade": grade,
        "grade_reasons": grade_reasons,
        "intent": intent,
        "sql": sql,
        "params": params,
        "prompt_version": prompt_version,
        "sanitizer_verdict": sanitizer_verdict,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "cache_tier": cache_tier,
        "asker_id": asker_id,
        "via_clarify": via_clarify,
    }
    return append_jsonl(path, row)


def record_interaction(path, *, answer_id: str, interaction_kind: str, **extra) -> dict:
    if interaction_kind not in INTERACTION_KINDS:
        raise ValueError(f"unknown interaction kind {interaction_kind!r}; allowed: {sorted(INTERACTION_KINDS)}")
    row = {
        "kind": "interaction",
        "ts": now_iso(),
        "answer_id": answer_id,
        "interaction_kind": interaction_kind,
        **extra,
    }
    return append_jsonl(path, row)


def _read_jsonl(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _iso_week(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def summarize(path) -> dict:
    rows = _read_jsonl(path)
    answers = {r["answer_id"]: r for r in rows if r["kind"] == "answer"}
    interactions = [r for r in rows if r["kind"] == "interaction"]

    total = len(answers)
    by_grade: dict[str, list[dict]] = defaultdict(list)
    for a in answers.values():
        by_grade[a["grade"]].append(a)

    flags = [i for i in interactions if i["interaction_kind"] == "flag_incorrect_grade"]
    flag_rate_per_grade = {}
    for gr, rows_for_grade in by_grade.items():
        flagged = {i["answer_id"] for i in flags if answers.get(i["answer_id"], {}).get("grade") == gr}
        flag_rate_per_grade[gr] = len(flagged) / len(rows_for_grade) if rows_for_grade else 0.0

    export_ids = {i["answer_id"] for i in interactions if i["interaction_kind"] == "export_csv"}
    export_rate = len(export_ids) / total if total else 0.0

    certified = by_grade.get("CERTIFIED", [])
    certified_without_clarify = [a for a in certified if not a.get("via_clarify")]
    certified_without_clarify_share = len(certified_without_clarify) / total if total else 0.0

    active: dict[str, set] = defaultdict(set)
    for a in answers.values():
        if a.get("asker_id") is not None:
            active[_iso_week(a["ts"])].add(a["asker_id"])
    weekly_active_askers = {week: len(ids) for week, ids in active.items()}

    return {
        "total_answers": total,
        "flag_rate_per_grade": flag_rate_per_grade,
        "export_rate": export_rate,
        "certified_without_clarify_share": certified_without_clarify_share,
        "weekly_active_askers": weekly_active_askers,
    }


def reconstruct_flag(path, *, answer_id: str) -> dict | None:
    """Everything needed to debug a `flag_incorrect_grade`: the exact
    intent, SQL and grade inputs behind the flagged answer, plus the flag(s)
    filed against it.
    """
    rows = _read_jsonl(path)
    answer = next((r for r in rows if r["kind"] == "answer" and r["answer_id"] == answer_id), None)
    if answer is None:
        return None
    flags = [
        r for r in rows
        if r["kind"] == "interaction" and r["answer_id"] == answer_id and r["interaction_kind"] == "flag_incorrect_grade"
    ]
    return {"answer": answer, "flags": flags}

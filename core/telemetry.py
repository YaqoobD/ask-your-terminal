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
        f.write(json.dumps(row, default=str) + "\n")
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


def admin_snapshot(path, *, recent_limit: int = 30) -> dict:
    """Operator-facing view over the same log `summarize()` reads: cost and
    latency per pipeline stage, cache-tier hit rates, grade distribution, and
    the most recent answers with enough detail to audit one by hand.
    """
    rows = _read_jsonl(path)
    answers = [r for r in rows if r["kind"] == "answer"]

    total_cost = sum(a.get("cost_usd") or 0.0 for a in answers)
    tokens_by_stage: dict[str, dict[str, int]] = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0})
    latency_sum: dict[str, int] = defaultdict(int)
    latency_count: dict[str, int] = defaultdict(int)
    grade_counts: dict[str, int] = defaultdict(int)
    cache_counts: dict[str, int] = defaultdict(int)

    for a in answers:
        grade_counts[a["grade"]] += 1
        cache_counts[a.get("cache_tier") or "none"] += 1
        for stage, usage in (a.get("tokens") or {}).items():
            tokens_by_stage[stage]["input_tokens"] += usage.get("input_tokens", 0)
            tokens_by_stage[stage]["output_tokens"] += usage.get("output_tokens", 0)
        for stage, ms in (a.get("latency_ms") or {}).items():
            latency_sum[stage] += ms
            latency_count[stage] += 1

    avg_latency_ms = {stage: round(latency_sum[stage] / latency_count[stage]) for stage in latency_sum}

    recent = sorted(answers, key=lambda a: a["ts"], reverse=True)[:recent_limit]
    recent_view = [
        {
            "ts": a["ts"],
            "answer_id": a["answer_id"],
            "tenant_id": a["tenant_id"],
            "grade": a["grade"],
            "metric": (a.get("intent") or {}).get("metric"),
            "time_window": (a.get("intent") or {}).get("time_window"),
            "prompt_version": a.get("prompt_version"),
            "sanitizer_verdict": a.get("sanitizer_verdict"),
            "cache_tier": a.get("cache_tier"),
            "cost_usd": a.get("cost_usd"),
            "tokens": a.get("tokens"),
            "latency_ms": a.get("latency_ms"),
        }
        for a in recent
    ]

    return {
        "total_answers": len(answers),
        "total_cost_usd": round(total_cost, 6),
        "tokens_by_stage": dict(tokens_by_stage),
        "avg_latency_ms": avg_latency_ms,
        "grade_counts": dict(grade_counts),
        "cache_tier_counts": dict(cache_counts),
        "recent": recent_view,
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

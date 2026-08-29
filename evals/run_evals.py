"""Five-layer eval harness. Layers 1 to 4 gate `make evals`; layer 5
(narration faithfulness) is judged by a model and reports only, it never
blocks. Layers 1, 3 and 4 exercise the real intent-extraction provider, so a
run costs live model calls; that is the same tradeoff Phase 4's exit check
already made, this only extends it into a repeatable scoreboard.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from core.cache import compute_watermark
from core.compile import CompileError, TenantScopeError, compile as compile_intent
from core.diagnose import diagnose
from core.extract import extract
from core.grade import Grade, GradeInputs, Refusal, grade
from core.intent import QueryIntent
from core.narrate import NarrationError, _evidence_block, narrate
from core.providers import get_provider
from core.timewindow import TimeWindowError, resolve_time_window
from registry.load import DB_PATH, resolve

ROOT = Path(__file__).parent.parent
EVALS_DIR = Path(__file__).parent

# The synthetic dataset ends 2026-06-26 (data/generate.py: START + 8 weeks).
# Pinning "now" just past that keeps the freshness and correction-window
# traps deterministic, instead of drifting further into the dataset's past
# every day this harness happens to run in real wall-clock time.
EVAL_NOW = datetime(2026, 6, 27)
SETTLE_DAYS = 7  # > the 6-day spike knowledge delay planted in data/generate.py

SNAPSHOT_PATH = EVALS_DIR / "compiler_snapshots.json"
SNAPSHOT_INTENTS = [
    ("tos_alpha", QueryIntent(op="aggregate", metric="dwell_time", time_window="week 3")),
    ("tos_alpha", QueryIntent(op="aggregate", metric="dwell_time", dimensions=["berth"], time_window="week 3")),
    ("tos_beta", QueryIntent(op="aggregate", metric="crane_idle_pct", dimensions=["equipment_type"], time_window="week 2")),
    ("tos_alpha", QueryIntent(op="aggregate", metric="moves_per_hr", time_window="week 4")),
    ("tos_beta", QueryIntent(op="aggregate", metric="gate_turnaround", time_window="week 1")),
]

JUDGE_SYSTEM = (
    "You check whether a narration is faithful to an evidence block: every number "
    "in the narration must appear in the evidence, and it must not claim causation "
    "for a contributor. Respond with exactly one JSON object, no prose: "
    '{"faithful": true or false, "reason": "<one sentence>"}'
)


@dataclass
class CaseResult:
    label: str
    passed: bool
    detail: str


@dataclass
class LayerResult:
    name: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run_layer1_intent() -> LayerResult:
    layer = LayerResult("1 - Intent")
    for case in _read_jsonl(EVALS_DIR / "gold_intents.jsonl"):
        registry = resolve(case["tenant"])
        result = extract(case["question"], registry)
        if result.intent is None:
            layer.cases.append(CaseResult(
                case["question"], False,
                f"no intent produced (clarify={result.clarify!r}, refusal={result.refusal!r})",
            ))
            continue
        got = result.intent.model_dump(exclude_none=True)
        mismatches = []
        for key, expected in case["expected"].items():
            got_value = got.get(key)
            if key == "dimensions":
                got_value, expected = sorted(got_value or []), sorted(expected)
            if got_value != expected:
                mismatches.append(f"{key}: expected {expected!r}, got {got_value!r}")
        layer.cases.append(CaseResult(case["question"], not mismatches, "; ".join(mismatches) or "ok"))
    return layer


def _snapshot_id(tenant: str, intent: QueryIntent) -> str:
    return f"{tenant}:{intent.metric}:{','.join(intent.dimensions)}"


def run_layer2_compiler() -> LayerResult:
    layer = LayerResult("2 - Compiler")
    snapshots = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else {}
    changed = False
    for tenant, intent in SNAPSHOT_INTENTS:
        key = _snapshot_id(tenant, intent)
        cq = compile_intent(intent, tenant_id=tenant)
        if key not in snapshots:
            snapshots[key] = cq.sql
            changed = True
            layer.cases.append(CaseResult(key, True, "snapshot created on this run"))
            continue
        match = snapshots[key] == cq.sql
        layer.cases.append(CaseResult(key, match, "matches snapshot" if match else "compiled SQL drifted from snapshot"))
    if changed:
        SNAPSHOT_PATH.write_text(json.dumps(snapshots, indent=2, sort_keys=True) + "\n")
    return layer


def run_layer3_end_to_end() -> LayerResult:
    layer = LayerResult("3 - End to end")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        for case in _read_jsonl(EVALS_DIR / "gold_answers.jsonl"):
            registry = resolve(case["tenant"])
            result = extract(case["question"], registry)
            if result.intent is None:
                layer.cases.append(CaseResult(case["question"], False, f"no intent (clarify={result.clarify!r})"))
                continue
            try:
                cq = compile_intent(result.intent, tenant_id=case["tenant"], registry=registry)
                value = con.execute(cq.sql, cq.params).fetchone()[-1]
            except Exception as exc:
                layer.cases.append(CaseResult(case["question"], False, f"compile/execute failed: {exc}"))
                continue
            diff = abs(value - case["expected_value"])
            ok = diff <= case["tolerance"]
            detail = f"got {value:.2f}, expected {case['expected_value']:.2f}"
            if not ok:
                detail += f", diff {diff:.2f} exceeds tolerance {case['tolerance']}"
            layer.cases.append(CaseResult(case["question"], ok, detail))
    finally:
        con.close()
    return layer


def _grade_intent(intent: QueryIntent, tenant_id: str, registry, con: duckdb.DuckDBPyConnection) -> tuple[Grade, str]:
    metric = registry.metrics[intent.metric]
    try:
        cq = compile_intent(intent, tenant_id=tenant_id, registry=registry)
    except TenantScopeError as exc:
        return grade(GradeInputs(refusal=Refusal(reason=str(exc), source="tenant"))).grade, str(exc)
    except (CompileError, TimeWindowError) as exc:
        return grade(GradeInputs(refusal=Refusal(reason=str(exc), source="compile"))).grade, str(exc)

    con.execute(cq.sql, cq.params)
    _, window_end = resolve_time_window(intent.time_window)
    closed = EVAL_NOW >= window_end + timedelta(days=SETTLE_DAYS)
    watermark = compute_watermark(intent.metric, registry, con)
    freshness_hours = (
        (EVAL_NOW - datetime.fromisoformat(watermark)).total_seconds() / 3600
        if watermark != "unknown" else None
    )
    result = grade(GradeInputs(
        freshness_hours=freshness_hours,
        freshness_sla_hours=metric["freshness_sla_hours"],
        completeness_pct=100.0,
        window_closed_to_corrections=closed,
    ))
    return result.grade, "; ".join(result.reasons)


def run_layer4_traps() -> LayerResult:
    layer = LayerResult("4 - Must-refuse / must-clarify")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        for case in _read_jsonl(EVALS_DIR / "traps.jsonl"):
            registry = resolve(case["tenant"])
            result = extract(case["question"], registry)
            if result.refusal is not None:
                got, detail = Grade.REFUSE, f"refused by {result.refusal.source}: {result.refusal.reason}"
            elif result.clarify is not None:
                got, detail = Grade.CLARIFY, f"clarify: {result.clarify}"
            else:
                got, why = _grade_intent(result.intent, case["tenant"], registry, con)
                detail = f"graded {got.value}: {why}"
            expected = Grade[case["expected_grade"]]
            ok = got == expected
            if not ok:
                detail = f"expected {expected.value}, got {got.value} ({detail})"
            layer.cases.append(CaseResult(case["question"], ok, detail))
    finally:
        con.close()
    return layer


def run_layer5_narration() -> LayerResult:
    layer = LayerResult("5 - Narration faithfulness (reports only)")
    registry = resolve("tos_beta")
    intent = QueryIntent(op="diagnose", metric="dwell_time", time_window="week 5")
    result = diagnose(intent, tenant_id="tos_beta", registry=registry)
    try:
        text = narrate(result)
    except NarrationError as exc:
        layer.cases.append(CaseResult("week 5 spike narration", False, f"narration rejected: {exc}"))
        return layer
    judge = get_provider("narrate")
    raw = judge.complete(
        system=JUDGE_SYSTEM,
        user=f"Evidence:\n{_evidence_block(result)}\n\nNarration:\n{text}",
    )
    match = re.search(r"\{.*\}", raw, re.S)
    verdict = json.loads(match.group(0)) if match else {"faithful": None, "reason": "judge response unparseable"}
    layer.cases.append(CaseResult("week 5 spike narration", bool(verdict.get("faithful")), verdict.get("reason", "")))
    return layer


def _record(layer: LayerResult, lines: list[str]) -> None:
    passed_count = sum(c.passed for c in layer.cases)
    status = "PASS" if layer.passed else "FAIL"
    print(f"{layer.name}: {status} ({passed_count}/{len(layer.cases)})")
    lines.append(f"## {layer.name}")
    lines.append(f"**{status}**, {passed_count}/{len(layer.cases)} cases.\n")
    for c in layer.cases:
        mark = "PASS" if c.passed else "FAIL"
        print(f"  [{mark}] {c.label}: {c.detail}")
        lines.append(f"- [{mark}] `{c.label}`: {c.detail}")
    lines.append("")


def main() -> int:
    report = ["# Eval report\n"]
    gating_layers = [run_layer1_intent(), run_layer2_compiler(), run_layer3_end_to_end(), run_layer4_traps()]
    for layer in gating_layers:
        _record(layer, report)

    _record(run_layer5_narration(), report)

    gate_pass = all(layer.passed for layer in gating_layers)
    print()
    print("GATE (layers 1 to 4):", "PASS" if gate_pass else "FAIL")
    report.append(f"**Gate (layers 1 to 4): {'PASS' if gate_pass else 'FAIL'}**")
    (EVALS_DIR / "report.md").write_text("\n".join(report) + "\n")

    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())

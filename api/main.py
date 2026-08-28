"""FastAPI surface: one question in, one graded answer card out.

POST /ask runs the full pipeline (sanitize -> extract -> compile -> execute
-> grade -> telemetry), reusing every core module exactly as Phase 4 to 7
built it; this file adds no new business logic, only wiring and an HTTP
shape. POST /event is the only other writer path and appends interaction
records through core.telemetry, unchanged. GET /health is a liveness probe.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.cache import AnswerCache, IntentCache, compute_watermark, intent_hash
from core.compile import CompileError, TenantScopeError, compile as compile_intent
from core.diagnose import diagnose
from core.extract import extract
from core.grade import Grade, GradeInputs, Refusal, grade
from core.narrate import NarrationError, narrate
from core.telemetry import record_answer, record_interaction
from core.tenancy import Session, UnknownSessionError, log_cross_tenant_attempt, resolve_tenant
from core.timewindow import TimeWindowError, resolve_time_window
from registry.load import DB_PATH, resolve

ROOT = Path(__file__).parent.parent
WEB_DIR = ROOT / "web"
RUNS_PATH = ROOT / "runs.jsonl"

# The synthetic dataset ends 2026-06-26 (data/generate.py). Pinning "now" just
# past the dataset's last week keeps freshness and correction-window grading
# demoable; the eval harness pins the same value for the same reason.
DEMO_NOW = datetime(2026, 6, 27)
SETTLE_DAYS = 7

SESSIONS = {
    "demo-alpha": Session(tenant_id="tos_alpha"),
    "demo-beta": Session(tenant_id="tos_beta"),
}

intent_cache = IntentCache()
answer_cache = AnswerCache()

app = FastAPI(title="Ask Your Terminal")


class AskRequest(BaseModel):
    question: str
    session_token: str
    asker_id: str | None = None


class EventRequest(BaseModel):
    answer_id: str
    interaction_kind: str
    extra: dict = {}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _restatement(intent) -> str:
    parts = [f"metric: {intent.metric}", f"window: {intent.time_window}"]
    if intent.dimensions:
        parts.append(f"by: {', '.join(intent.dimensions)}")
    if intent.filters:
        parts.append(f"where: {intent.filters}")
    return " · ".join(parts)


def _refuse(reason: str, source: str) -> tuple[Grade, list[str]]:
    result = grade(GradeInputs(refusal=Refusal(reason=reason, source=source)))
    return result.grade, result.reasons


def _diagnose_payload(intent, tenant_id: str, registry) -> dict:
    result = diagnose(intent, tenant_id=tenant_id, registry=registry)
    try:
        narration = narrate(result)
    except NarrationError as exc:
        narration = f"(narration withheld: {exc})"
    return {
        "base_value": result.base_value,
        "comparison_value": result.comparison_value,
        "total_delta": result.total_delta,
        "reality_check": {"is_artefact": result.reality.is_artefact, "reason": result.reality.reason},
        "dimensions": [
            {
                "dimension": d.dimension,
                "unexplained_remainder": d.unexplained_remainder,
                "contributions": [
                    {"level": c.level, "numerator_delta": c.numerator_delta, "share": c.share}
                    for c in d.contributions
                ],
            }
            for d in result.dimensions
        ],
        "narration": narration,
    }


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    try:
        tenant_id = resolve_tenant(req.session_token, SESSIONS)
    except UnknownSessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    answer_id = str(uuid.uuid4())
    registry = resolve(tenant_id)

    cached = intent_cache.get(req.question)
    cache_tier = "T1" if cached is not None else None
    result = cached if cached is not None else extract(req.question, registry)
    if cached is None:
        intent_cache.set(req.question, result)

    payload = {"answer_id": answer_id, "question": req.question, "cache_tier": cache_tier}

    if result.refusal is not None:
        g, reasons = _refuse(result.refusal.reason, result.refusal.source)
        payload.update(grade=g.value, reasons=reasons)
    elif result.clarify is not None:
        gr = grade(GradeInputs(clarify=result.clarify))
        payload.update(grade=gr.grade.value, reasons=gr.reasons, clarify=result.clarify)
    else:
        intent = result.intent
        try:
            cq = compile_intent(intent, tenant_id=tenant_id, registry=registry)
        except TenantScopeError as exc:
            log_cross_tenant_attempt(RUNS_PATH, tenant_id=tenant_id, attempted_filter=intent.filters, reason=str(exc))
            g, reasons = _refuse(str(exc), "tenant")
            payload.update(grade=g.value, reasons=reasons)
        except (CompileError, TimeWindowError) as exc:
            g, reasons = _refuse(str(exc), "compile")
            payload.update(grade=g.value, reasons=reasons)
        else:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            try:
                watermark = compute_watermark(intent.metric, registry, con)
                ih = intent_hash(intent)
                cached_answer = answer_cache.get(ih, tenant_id, watermark)
                if cached_answer is not None:
                    rows, columns = cached_answer
                    cache_tier = "T0"
                else:
                    cursor = con.execute(cq.sql, cq.params)
                    columns = [d[0] for d in cursor.description]
                    rows = cursor.fetchall()
                    answer_cache.set(ih, tenant_id, watermark, (rows, columns))

                _, window_end = resolve_time_window(intent.time_window)
                closed = DEMO_NOW >= window_end + timedelta(days=SETTLE_DAYS)
                freshness_hours = (
                    (DEMO_NOW - datetime.fromisoformat(watermark)).total_seconds() / 3600
                    if watermark != "unknown" else None
                )
                metric = registry.metrics[intent.metric]
                gr = grade(GradeInputs(
                    freshness_hours=freshness_hours,
                    freshness_sla_hours=metric["freshness_sla_hours"],
                    completeness_pct=100.0,
                    window_closed_to_corrections=closed,
                ))
                payload.update(
                    grade=gr.grade.value,
                    reasons=gr.reasons,
                    restatement=_restatement(intent),
                    definition=cq.definition,
                    as_of=watermark,
                    completeness_pct=100.0,
                    columns=columns,
                    rows=[list(r) for r in rows],
                    sql=cq.sql,
                    params=cq.params,
                    lineage=cq.lineage,
                )
                if intent.op == "diagnose":
                    payload["diagnose"] = _diagnose_payload(intent, tenant_id, registry)
            finally:
                con.close()

    record_answer(
        RUNS_PATH,
        answer_id=answer_id,
        intent_hash=intent_hash(result.intent) if result.intent else "",
        tenant_id=tenant_id,
        grade=payload["grade"],
        grade_reasons=payload["reasons"],
        intent=result.intent.model_dump(exclude_none=True) if result.intent else {},
        sql=payload.get("sql", ""),
        params=payload.get("params", []),
        prompt_version="intent_v1",
        sanitizer_verdict="block" if (result.refusal and result.refusal.source == "sanitize") else "pass",
        tokens={},
        cost_usd=0.0,
        latency_ms={},
        cache_tier=cache_tier,
        asker_id=req.asker_id,
        via_clarify=False,
    )
    return payload


@app.post("/event")
def event(req: EventRequest) -> dict:
    return record_interaction(RUNS_PATH, answer_id=req.answer_id, interaction_kind=req.interaction_kind, **req.extra)


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

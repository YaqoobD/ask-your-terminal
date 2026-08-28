import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from core.cache import AnswerCache, IntentCache, PromptCache, compute_watermark, intent_hash, normalize_question
from core.intent import QueryIntent
from registry.load import resolve

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "terminal.duckdb"


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    if not DB_PATH.exists():
        subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True)
    yield


def test_normalize_question_collapses_rephrasings():
    assert normalize_question("  What was  Dwell Time last week?") == normalize_question("what was dwell time last week?")


def test_two_rephrasings_hit_t1():
    cache = IntentCache()
    intent = QueryIntent(op="aggregate", metric="dwell_time", time_window="week 3")
    cache.set("What was dwell time last week?", intent)
    assert cache.get("what   was dwell time last week?  ") is intent
    assert cache.get("Something entirely different?") is None


def test_t0_hits_on_repeated_intent_same_watermark():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    resolved = resolve("tos_alpha")
    watermark = compute_watermark("dwell_time", resolved, con)
    con.close()

    intent = QueryIntent(op="aggregate", metric="dwell_time", time_window="week 3")
    h = intent_hash(intent)

    cache = AnswerCache()
    assert cache.get(h, "tos_alpha", watermark) is None
    cache.set(h, "tos_alpha", watermark, {"value": 42})
    assert cache.get(h, "tos_alpha", watermark) == {"value": 42}


def test_correction_bumps_watermark_only_for_affected_metric(tmp_path):
    # Work on a private copy so this test never mutates the shared fixture db.
    scratch_path = tmp_path / "scratch.duckdb"
    con = duckdb.connect(str(scratch_path))
    con.execute(f"attach '{DB_PATH}' as src")
    for table in ["container_events", "equipment_downtime"]:
        con.execute(f"create table {table} as select * from src.{table}")
    con.execute("detach src")

    resolved = resolve("tos_alpha")

    dwell_watermark_before = compute_watermark("dwell_time", resolved, con)
    idle_watermark_before = compute_watermark("crane_idle_pct", resolved, con)

    # A correction lands in equipment_downtime (crane_idle_pct's numerator table),
    # not in container_events (dwell_time's).
    future_knowledge_time = datetime.now() + timedelta(days=1)
    con.execute(
        "insert into equipment_downtime (downtime_id, terminal, equipment_id, event_time, "
        "duration_hours, knowledge_time) values (?, ?, ?, ?, ?, ?)",
        [999999999, "terminal_a", "CRANE-99", datetime.now(), 1.5, future_knowledge_time],
    )

    dwell_watermark_after = compute_watermark("dwell_time", resolved, con)
    idle_watermark_after = compute_watermark("crane_idle_pct", resolved, con)
    con.close()

    assert dwell_watermark_after == dwell_watermark_before
    assert idle_watermark_after != idle_watermark_before


def test_prompt_cache_builds_once_per_client():
    cache = PromptCache()
    calls = []

    def build():
        calls.append(1)
        return "registry block"

    assert cache.get_or_build("tos_alpha", build) == "registry block"
    assert cache.get_or_build("tos_alpha", build) == "registry block"
    assert len(calls) == 1

    cache.get_or_build("tos_beta", build)
    assert len(calls) == 2

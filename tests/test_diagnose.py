import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from core.diagnose import (
    DiagnoseResult,
    DimensionResult,
    RealityCheck,
    SignalCorrelation,
    check_correction_replay,
    diagnose,
)
from core.intent import QueryIntent
from core.narrate import NarrationError, narrate, verify_numbers_in_evidence
from registry.load import resolve

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "terminal.duckdb"

# Must match data/generate.py.
SPIKE_TERMINAL_TENANT = "tos_beta"
SPIKE_WEEK = 5


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    if not DB_PATH.exists():
        subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True)
    yield


@pytest.fixture
def registry():
    return resolve(SPIKE_TERMINAL_TENANT)


def test_planted_reefer_spike_ranks_first_by_contribution(registry):
    intent = QueryIntent(op="diagnose", metric="dwell_time", time_window=f"week {SPIKE_WEEK}")
    result = diagnose(intent, tenant_id=SPIKE_TERMINAL_TENANT, registry=registry)

    reefer_dim = next(d for d in result.dimensions if d.dimension == "is_reefer")
    top = reefer_dim.contributions[0]
    assert top.level == "True"
    assert top.numerator_delta > 0
    # The rest of the levels combined contribute far less than the reefer level.
    rest = sum(abs(c.numerator_delta) for c in reefer_dim.contributions[1:])
    assert abs(top.numerator_delta) > rest

    assert result.reality.is_artefact is False


def test_remainder_is_always_reported(registry):
    intent = QueryIntent(op="diagnose", metric="dwell_time", time_window=f"week {SPIKE_WEEK}")
    result = diagnose(intent, tenant_id=SPIKE_TERMINAL_TENANT, registry=registry)
    for dim in result.dimensions:
        assert hasattr(dim, "unexplained_remainder")
        assert isinstance(dim.unexplained_remainder, (int, float))

    # A window with no known spike still reports a remainder, even near zero.
    quiet_intent = QueryIntent(op="diagnose", metric="dwell_time", time_window="week 1")
    quiet_result = diagnose(quiet_intent, tenant_id=SPIKE_TERMINAL_TENANT, registry=registry)
    for dim in quiet_result.dimensions:
        assert isinstance(dim.unexplained_remainder, (int, float))


@pytest.mark.parametrize("intent_kwargs", [
    dict(metric="dwell_time", time_window=f"week {SPIKE_WEEK}"),
    dict(metric="dwell_time", time_window="week 1"),
    dict(metric="dwell_time", time_window="week 3"),
    dict(metric="crane_idle_pct", time_window="week 2"),
    dict(metric="gate_turnaround", time_window="week 4"),
])
def test_closure_identity_contributions_plus_remainder_equals_total(registry, intent_kwargs):
    intent = QueryIntent(op="diagnose", **intent_kwargs)
    result = diagnose(intent, tenant_id=SPIKE_TERMINAL_TENANT, registry=registry)
    for dim in result.dimensions:
        explained = sum(c.numerator_delta for c in dim.contributions)
        assert explained + dim.unexplained_remainder == pytest.approx(dim.total_numerator_delta, abs=1e-6)
        assert dim.total_numerator_delta == pytest.approx(result.total_delta, abs=1e-6)


def test_closure_identity_random_windows_property(registry):
    rng = random.Random(7)
    for _ in range(15):
        week = rng.randint(1, 6)
        intent = QueryIntent(op="diagnose", metric="dwell_time", time_window=f"week {week}")
        result = diagnose(intent, tenant_id=SPIKE_TERMINAL_TENANT, registry=registry)
        for dim in result.dimensions:
            explained = sum(c.numerator_delta for c in dim.contributions)
            assert explained + dim.unexplained_remainder == pytest.approx(dim.total_numerator_delta, abs=1e-6)


def test_correction_replay_survives_a_real_spike():
    """The planted reefer spike is a genuine event reported late, not a
    duplicate-row artefact: it should survive dedup near-untouched."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        start = datetime(2026, 5, 1) + timedelta(weeks=SPIKE_WEEK)
        end = start + timedelta(weeks=1)
        check = check_correction_replay(
            con,
            table="container_events",
            value_expr="date_diff('hour', gate_in, gate_out)",
            agg="sum",
            time_col="gate_in",
            start=start,
            end=end,
            key_col="container_id",
            knowledge_col="gate_out_knowledge_time",
            tenant_value="terminal_b",
            soft_delete_col="deleted",
        )
        assert check.is_artefact is False
    finally:
        con.close()


def test_correction_replay_flags_a_duplicate_only_artefact():
    """A synthetic case built to be nothing but duplicate rows: the raw total
    is inflated purely by re-sent copies, so it must not survive dedup."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            "create table fake_events (id bigint, terminal varchar, event_time timestamp, "
            "knowledge_time timestamp, value double, deleted boolean)"
        )
        base = datetime(2026, 1, 1)
        rows = []
        for i in range(10):
            rows.append((i, "terminal_x", base, base, 10.0, False))
            # Every row re-sent 4 more times with a later knowledge_time and the same value.
            for copy in range(1, 5):
                rows.append((i, "terminal_x", base, base + timedelta(hours=copy), 10.0, False))
        con.executemany("insert into fake_events values (?, ?, ?, ?, ?, ?)", rows)

        check = check_correction_replay(
            con,
            table="fake_events",
            value_expr="value",
            agg="sum",
            time_col="event_time",
            start=base - timedelta(days=1),
            end=base + timedelta(days=1),
            key_col="id",
            knowledge_col="knowledge_time",
            tenant_value="terminal_x",
            soft_delete_col="deleted",
        )
        assert check.is_artefact is True
        assert check.raw_value == pytest.approx(500.0)
        assert check.deduped_value == pytest.approx(100.0)
    finally:
        con.close()


def _fake_result() -> DiagnoseResult:
    return DiagnoseResult(
        metric="dwell_time",
        base_value=39.0,
        comparison_value=60.0,
        total_delta=1200.0,
        reality=RealityCheck(is_artefact=False, reason="survives correction replay", raw_value=1200.0, deduped_value=1190.0),
        dimensions=[
            DimensionResult(
                dimension="is_reefer",
                contributions=[],
                total_numerator_delta=1200.0,
                unexplained_remainder=50.0,
            ),
        ],
        signals=[SignalCorrelation(signal="downtime_hours_sum", base_value=5.0, comparison_value=8.0, delta=3.0)],
    )


class MockProvider:
    def __init__(self, response: str):
        self.response = response

    def complete(self, *, system: str, user: str) -> str:
        return self.response


def test_narration_with_only_evidence_numbers_passes():
    result = _fake_result()
    provider = MockProvider(
        "Dwell time rose from 39 to 60 hours, a delta of 1200. The is_reefer dimension "
        "leaves an unexplained remainder of 50. Downtime rose by 3."
    )
    text = narrate(result, provider=provider)
    assert "50" in text


def test_narration_with_an_invented_number_is_rejected():
    result = _fake_result()
    provider = MockProvider("Dwell time rose by roughly 1500 hours, driven mostly by reefers.")
    with pytest.raises(NarrationError):
        narrate(result, provider=provider)


def test_verify_numbers_in_evidence_direct():
    result = _fake_result()
    verify_numbers_in_evidence("Delta of 1200, remainder 50.", result)
    with pytest.raises(NarrationError):
        verify_numbers_in_evidence("Delta of 999999.", result)

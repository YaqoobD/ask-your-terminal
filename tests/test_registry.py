import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from registry import from_tmdl, load

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "terminal.duckdb"
TMDL_PATH = ROOT / "registry" / "samples" / "terminal_model.tmdl"


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    if not DB_PATH.exists():
        subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True)
    yield


def test_both_clients_resolve_against_seeded_db():
    alpha = load.resolve("tos_alpha", db_path=DB_PATH)
    beta = load.resolve("tos_beta", db_path=DB_PATH)

    assert alpha.terminal == "terminal_a"
    assert beta.terminal == "terminal_b"
    assert set(alpha.metrics) == set(load.load_canonical()["metrics"])
    assert alpha.metrics == beta.metrics  # same governed semantics, different mapping


def test_broken_column_override_raises():
    canonical = load.load_canonical()
    client = load.load_client("tos_alpha")
    client = {**client, "column_overrides": {"downtime_hours_sum": {"table": "equipment_downtime", "column": "not_a_real_column"}}}
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        with pytest.raises(load.RegistryError):
            load.resolve_from_dicts(canonical, client, con)
    finally:
        con.close()


def test_broken_soft_delete_table_raises():
    canonical = load.load_canonical()
    client = load.load_client("tos_alpha")
    client = {**client, "soft_delete_columns": {"not_a_real_table": "deleted"}}
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        with pytest.raises(load.RegistryError):
            load.resolve_from_dicts(canonical, client, con)
    finally:
        con.close()


def test_unknown_declared_signal_raises():
    canonical = load.load_canonical()
    client = load.load_client("tos_alpha")
    client = {**client, "declared_signals": ["not_a_real_signal"]}
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        with pytest.raises(load.RegistryError):
            load.resolve_from_dicts(canonical, client, con)
    finally:
        con.close()


def test_tmdl_parser_produces_expected_metric_entries():
    measures = from_tmdl.parse_tmdl(TMDL_PATH)
    names = {m["name"] for m in measures}
    assert names == {"Dwell Time", "Crane Idle %", "Gate Turnaround"}
    dwell = next(m for m in measures if m["name"] == "Dwell Time")
    assert dwell["metric_key"] == "dwell_time"
    assert dwell["description"] == "Average hours a container spends between gate-in and gate-out."


def test_drift_flags_disagreeing_metric_only():
    canonical = load.load_canonical()
    measures = from_tmdl.parse_tmdl(TMDL_PATH)
    drift = load.check_drift(canonical, measures)

    flagged = {d["metric"] for d in drift}
    assert flagged == {"crane_idle_pct"}
    assert "dwell_time" not in flagged
    assert "gate_turnaround" not in flagged

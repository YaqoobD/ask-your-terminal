import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "terminal.duckdb"
TRUTH_PATH = ROOT / "data" / "known_truth.json"


@pytest.fixture(scope="module", autouse=True)
def regenerate():
    subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True)
    yield


def test_row_counts_stable_across_reruns():
    con = duckdb.connect(str(DB_PATH))
    counts_first = {
        t: con.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("vessel_calls", "container_events", "crane_moves")
    }
    con.close()

    subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True)

    con = duckdb.connect(str(DB_PATH))
    counts_second = {
        t: con.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("vessel_calls", "container_events", "crane_moves")
    }
    con.close()
    assert counts_first == counts_second


def test_every_table_has_both_clocks():
    con = duckdb.connect(str(DB_PATH))
    tables = [
        "vessel_calls",
        "berth_allocations",
        "crane_moves",
        "container_events",
        "gate_moves",
        "equipment_downtime",
    ]
    for table in tables:
        cols = {row[1] for row in con.execute(f"pragma table_info('{table}')").fetchall()}
        knowledge_cols = {c for c in cols if "knowledge_time" in c}
        event_cols = {c for c in cols if c in ("event_time", "gate_in")}
        assert knowledge_cols, f"{table} missing a knowledge_time column"
        assert event_cols, f"{table} missing an event_time column"
    con.close()


def test_planted_spike_present_in_latest_state():
    truth = json.loads(TRUTH_PATH.read_text())
    spike = truth["spike"]
    assert spike["avg_dwell_hours_latest"] is not None
    assert spike["avg_dwell_hours_latest"] > spike["avg_dwell_hours_before_knowledge_delay"] + 10


def test_planted_spike_absent_before_knowledge_delay():
    con = duckdb.connect(str(DB_PATH))
    truth = json.loads(TRUTH_PATH.read_text())
    spike = truth["spike"]

    as_of = con.execute(
        """
        select avg(date_diff('hour', gate_in, gate_out))
        from container_events
        where deleted = false
          and terminal = ?
          and is_reefer = true
          and (date_diff('day', (select min(event_time) from vessel_calls), gate_in) / 7)::int = ?
          and gate_out_knowledge_time <= (select min(event_time) from vessel_calls) + interval (7 * (?+1)) days
        """,
        [spike["terminal"], spike["week_index"], spike["week_index"]],
    ).fetchone()[0]
    con.close()

    assert as_of == pytest.approx(spike["avg_dwell_hours_before_knowledge_delay"])
    assert as_of < spike["avg_dwell_hours_latest"] - 10


def test_known_truth_matches_direct_sql():
    truth = json.loads(TRUTH_PATH.read_text())
    con = duckdb.connect(str(DB_PATH))
    for table, expected in truth["row_counts"].items():
        actual = con.execute(f"select count(*) from {table}").fetchone()[0]
        assert actual == expected, f"{table}: expected {expected}, got {actual}"
    con.close()


def test_injection_string_planted_in_remark():
    con = duckdb.connect(str(DB_PATH))
    truth = json.loads(TRUTH_PATH.read_text())
    hit = con.execute(
        "select count(*) from berth_allocations where remark like ?",
        [f"%{truth['injection_string']}%"],
    ).fetchone()[0]
    con.close()
    assert hit >= 1


def test_soft_delete_and_reversal_present():
    con = duckdb.connect(str(DB_PATH))
    deleted_count = con.execute("select count(*) from vessel_calls where deleted = true").fetchone()[0]
    con.close()
    assert deleted_count >= 1


def test_divergent_berth_naming_per_terminal():
    con = duckdb.connect(str(DB_PATH))
    styles = con.execute(
        "select terminal, berth from berth_allocations group by 1, 2"
    ).fetchall()
    con.close()
    a_style = any(b.startswith("B") and not b.startswith("BERTH") for t, b in styles if t == "terminal_a")
    b_style = any(b.startswith("BERTH-") for t, b in styles if t == "terminal_b")
    assert a_style and b_style


def test_duplicate_event_rows_exist():
    con = duckdb.connect(str(DB_PATH))
    dupes = con.execute(
        """
        select container_id, count(*) as n
        from container_events
        group by container_id
        having count(*) > 1
        """
    ).fetchall()
    con.close()
    assert len(dupes) >= 1

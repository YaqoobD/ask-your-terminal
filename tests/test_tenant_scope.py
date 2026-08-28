import re
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from core.compile import TenantScopeError, compile as compile_intent
from core.intent import QueryIntent
from registry.load import resolve

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "terminal.duckdb"


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    if not DB_PATH.exists():
        subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True)
    yield


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(str(DB_PATH), read_only=True)
    yield c
    c.close()


CANDIDATE_INTENTS = [
    QueryIntent(op="aggregate", metric="dwell_time", time_window="week 3"),
    QueryIntent(op="aggregate", metric="dwell_time", dimensions=["terminal", "is_reefer"], time_window="week 3"),
    QueryIntent(op="aggregate", metric="dwell_time", dimensions=["berth"], time_window="week 3"),
    QueryIntent(op="aggregate", metric="crane_idle_pct", dimensions=["equipment_type"], time_window="week 2"),
    QueryIntent(op="aggregate", metric="moves_per_hr", dimensions=["berth"], time_window="week 3"),
    QueryIntent(op="aggregate", metric="berth_productivity", time_window="week 4"),
    QueryIntent(op="aggregate", metric="gate_turnaround", time_window="week 1"),
]

_QMARK_TABLE_PREDICATE = re.compile(r"(\w+)\.terminal\s*=\s*\?")


@pytest.mark.parametrize("intent", CANDIDATE_INTENTS, ids=[i.metric for i in CANDIDATE_INTENTS])
@pytest.mark.parametrize("tenant_id", ["tos_alpha", "tos_beta"])
def test_every_base_relation_carries_a_bound_tenant_predicate(intent, tenant_id):
    cq = compile_intent(intent, tenant_id=tenant_id)
    resolved = resolve(tenant_id)

    tables_with_predicate = set(_QMARK_TABLE_PREDICATE.findall(cq.sql))
    assert tables_with_predicate == set(cq.lineage["tables"])

    # never interpolated: the terminal name must not appear as a literal in the
    # SQL text itself, only as a bound parameter value.
    assert resolved.terminal not in cq.sql
    assert cq.params.count(resolved.terminal) >= len(tables_with_predicate)


def test_same_intent_two_tenants_gives_non_overlapping_results():
    con_a = duckdb.connect(str(DB_PATH), read_only=True)
    intent = QueryIntent(op="aggregate", metric="dwell_time", dimensions=["terminal"], time_window="week 3")

    cq_alpha = compile_intent(intent, tenant_id="tos_alpha")
    cq_beta = compile_intent(intent, tenant_id="tos_beta")
    rows_alpha = con_a.execute(cq_alpha.sql, cq_alpha.params).fetchall()
    rows_beta = con_a.execute(cq_beta.sql, cq_beta.params).fetchall()
    con_a.close()

    terminals_alpha = {r[0] for r in rows_alpha}
    terminals_beta = {r[0] for r in rows_beta}
    assert terminals_alpha == {"terminal_a"}
    assert terminals_beta == {"terminal_b"}
    assert not terminals_alpha & terminals_beta


def test_filter_naming_another_tenants_terminal_raises():
    intent = QueryIntent(
        op="aggregate", metric="dwell_time", filters={"terminal": "terminal_a"}, time_window="week 3"
    )
    with pytest.raises(TenantScopeError):
        compile_intent(intent, tenant_id="tos_beta")


def test_filter_naming_another_tenants_berth_style_raises():
    intent = QueryIntent(op="aggregate", metric="dwell_time", filters={"berth": "B3"}, time_window="week 3")
    with pytest.raises(TenantScopeError):
        compile_intent(intent, tenant_id="tos_beta")


def test_own_terminal_filter_is_a_harmless_no_op():
    intent = QueryIntent(op="aggregate", metric="dwell_time", filters={"terminal": "terminal_a"}, time_window="week 3")
    compile_intent(intent, tenant_id="tos_alpha")  # must not raise


def test_registry_resolved_for_a_different_tenant_is_rejected():
    intent = QueryIntent(op="aggregate", metric="dwell_time", time_window="week 3")
    wrong_registry = resolve("tos_alpha")
    with pytest.raises(TenantScopeError):
        compile_intent(intent, tenant_id="tos_beta", registry=wrong_registry)

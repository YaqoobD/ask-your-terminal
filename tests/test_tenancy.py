import json
from pathlib import Path

import pytest

from core.compile import TenantScopeError, compile as compile_intent
from core.grade import Grade, GradeInputs, Refusal, grade
from core.intent import QueryIntent
from core.tenancy import Session, UnknownSessionError, log_cross_tenant_attempt, resolve_tenant

SESSIONS = {
    "tok-alpha": Session(tenant_id="tos_alpha", user_id="alice"),
    "tok-beta": Session(tenant_id="tos_beta", user_id="bob"),
}


def test_resolve_tenant_maps_session_to_tenant():
    assert resolve_tenant("tok-alpha", SESSIONS) == "tos_alpha"
    assert resolve_tenant("tok-beta", SESSIONS) == "tos_beta"


def test_unknown_session_raises():
    with pytest.raises(UnknownSessionError):
        resolve_tenant("tok-nope", SESSIONS)


def test_log_cross_tenant_attempt_writes_a_security_event(tmp_path):
    path = tmp_path / "runs.jsonl"
    log_cross_tenant_attempt(path, tenant_id="tos_beta", attempted_filter={"terminal": "terminal_a"}, reason="cross-tenant terminal")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "security"
    assert rows[0]["event"] == "cross_tenant_attempt"
    assert rows[0]["tenant_id"] == "tos_beta"


@pytest.mark.parametrize("filters", [
    {"terminal": "terminal_a"},
    {"berth": "B3"},
])
def test_asking_for_another_clients_terminal_refuses_regardless_of_phrasing(tmp_path, filters):
    session_token = "tok-beta"
    tenant_id = resolve_tenant(session_token, SESSIONS)
    intent = QueryIntent(op="aggregate", metric="dwell_time", filters=filters, time_window="week 3")

    try:
        compile_intent(intent, tenant_id=tenant_id)
        refusal = None
    except TenantScopeError as exc:
        refusal = Refusal(reason=str(exc), source="tenant")
        log_cross_tenant_attempt(tmp_path / "runs.jsonl", tenant_id=tenant_id, attempted_filter=filters, reason=str(exc))

    result = grade(GradeInputs(refusal=refusal))
    assert result.grade == Grade.REFUSE

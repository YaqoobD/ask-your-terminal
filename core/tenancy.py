"""Session to tenant_id resolution, and refusal logging. The isolation
itself is the Phase 3 compiler invariant (every base relation carries a
bound tenant predicate); this module only supplies the tenant_id argument
from an authenticated session and records every cross-tenant attempt as a
security event, so this is the module that answers "who tried to see whose
data" without being the module that enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.telemetry import append_jsonl, now_iso


class UnknownSessionError(Exception):
    """The session token has no known tenant mapping."""


@dataclass(frozen=True)
class Session:
    tenant_id: str
    user_id: str | None = None


def resolve_tenant(session_token: str, sessions: dict[str, Session]) -> str:
    session = sessions.get(session_token)
    if session is None:
        raise UnknownSessionError(f"unknown session token {session_token!r}")
    return session.tenant_id


def log_cross_tenant_attempt(path, *, tenant_id: str, attempted_filter: dict, reason: str) -> dict:
    row = {
        "kind": "security",
        "ts": now_iso(),
        "event": "cross_tenant_attempt",
        "tenant_id": tenant_id,
        "attempted_filter": attempted_filter,
        "reason": reason,
    }
    return append_jsonl(path, row)

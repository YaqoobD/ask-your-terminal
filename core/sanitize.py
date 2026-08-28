"""Deterministic screening, run at two boundaries: on the inbound question
before any provider call, and on any free-text warehouse column before it
enters an evidence block. No model involved, so hostile traffic costs
nothing.

verdict is "pass" (unchanged), "strip" (neutralised, annotation kept on the
card) or "block" (refused before extraction runs).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_MAX_LEN = 2000

# Instruction-injection: role headers, "ignore previous", fenced blocks,
# attempts to override as_of/tenant/client via assignment syntax.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)", re.I),
    re.compile(r"^\s*(system|assistant|user)\s*:", re.I | re.M),
    re.compile(r"```"),
    re.compile(r"\b(as_of|tenant|tenant_id|client|client_id)\s*[=:]", re.I),
    re.compile(r"you are now", re.I),
    re.compile(r"new instructions", re.I),
]

# Registry-vocabulary escape: phrased as an instruction to go around the
# registry rather than as a metric question.
_ESCAPE_PATTERNS = [
    re.compile(r"\braw\s+table", re.I),
    re.compile(r"\ball\s+clients?\b", re.I),
    re.compile(r"\bselect\s+\*\s+from\b", re.I),
    re.compile(r"\bdrop\s+table\b", re.I),
    re.compile(r"\bdelete\s+from\b", re.I),
    re.compile(r"\bshow\s+me\s+the\s+(database|schema|tables)\b", re.I),
]

_BASE64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
_UNICODE_ESCAPE_BLOB = re.compile(r"(\\u[0-9a-fA-F]{4}){4,}")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Screen:
    clean: str
    verdict: str  # "pass" | "strip" | "block"
    reason: str | None = None


def screen(text: str) -> Screen:
    if len(text) > _MAX_LEN:
        return Screen(clean=text[:_MAX_LEN], verdict="block", reason="input exceeds length bound")

    if _CONTROL_CHARS.search(text):
        return Screen(clean=text, verdict="block", reason="input contains control characters")

    normalized = unicodedata.normalize("NFKC", text)

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(normalized):
            return Screen(clean=text, verdict="block", reason="input contains an instruction-injection pattern")

    if _BASE64_BLOB.search(text) or _UNICODE_ESCAPE_BLOB.search(text):
        return Screen(clean=text, verdict="block", reason="input contains an encoded blob")

    for pattern in _ESCAPE_PATTERNS:
        if pattern.search(normalized):
            return Screen(clean=text, verdict="strip", reason="input phrased as a registry-vocabulary escape")

    return Screen(clean=text, verdict="pass", reason=None)

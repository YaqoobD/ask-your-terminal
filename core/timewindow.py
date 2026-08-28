"""Resolves a time_window spec ("last_complete_month", "week 12", "yesterday",
an explicit {start, end}) into concrete UTC bounds. All bounds are half-open:
[start, end).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta


class TimeWindowError(Exception):
    """A time_window spec could not be resolved."""


_WEEK_RE = re.compile(r"^week\s+(\d+)$")

# Must match data/generate.py START: the anchor "week 0" begins on.
EPOCH = datetime(2026, 5, 1)


def _start_of_day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day)


def resolve_time_window(spec: str | dict, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    if isinstance(spec, dict) or hasattr(spec, "start"):
        raw = spec if isinstance(spec, dict) else {"start": spec.start, "end": spec.end}
        try:
            start = datetime.fromisoformat(raw["start"])
            end = datetime.fromisoformat(raw["end"])
        except (KeyError, ValueError) as exc:
            raise TimeWindowError(f"invalid explicit time_window: {spec!r}") from exc
        if end <= start:
            raise TimeWindowError(f"time_window end must be after start: {spec!r}")
        return start, end

    if spec == "yesterday":
        today = _start_of_day(now)
        return today - timedelta(days=1), today

    if spec == "last_complete_month":
        first_of_this_month = date(now.year, now.month, 1)
        first_of_prev_month = date(
            now.year - 1 if now.month == 1 else now.year,
            12 if now.month == 1 else now.month - 1,
            1,
        )
        return (
            datetime.combine(first_of_prev_month, datetime.min.time()),
            datetime.combine(first_of_this_month, datetime.min.time()),
        )

    match = _WEEK_RE.match(spec)
    if match:
        week_index = int(match.group(1))
        start = EPOCH + timedelta(weeks=week_index)
        return start, start + timedelta(weeks=1)

    raise TimeWindowError(f"unrecognised time_window spec: {spec!r}")

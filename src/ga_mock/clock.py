"""Virtual, ANCHORED clock.

boondmanager-mock bases its clock on `time.time() + offset`; here the base is
FIXED, because `today`/`yesterday`/`NdaysAgo` are part of the v1beta API
surface: a wall-clock "today" would fall outside the generated world (which
stops at the anchor) and would return zero rows forever. The anchor reuses
boondmanager-mock's date (July 15, 2026) so that cross-source BI joins line up
in dev — and it makes every response deterministic down to the byte.

The offset (`clock.offset_seconds`) is driven by /__admin/clock and reset to
zero by `state.reset()`: advancing the clock ages relative dates, bearer
expiry and freshness visibility TOGETHER.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

ANCHOR = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone(timedelta(hours=2)))

_ISO_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_RELATIVE_PATTERN = re.compile(r"(\d+)daysAgo")


class Clock:
    def __init__(self) -> None:
        self.offset_seconds: float = 0.0


clock = Clock()


def paris_timezone(utc: datetime) -> timezone:
    """Simplified rule reused from boondmanager-mock: April-October = +02:00,
    otherwise +01:00. Off by a few days around real DST switchovers —
    assumed: no mock metric depends on the exact switchover time."""
    return timezone(timedelta(hours=2 if 4 <= utc.month <= 10 else 1))


def virtual_now() -> datetime:
    utc = (ANCHOR + timedelta(seconds=clock.offset_seconds)).astimezone(UTC)
    return utc.astimezone(paris_timezone(utc))


def virtual_today() -> date:
    return virtual_now().date()


def resolve_date(raw: str) -> date:
    """DateRange v1beta grammar: `YYYY-MM-DD`, `today`, `yesterday`,
    `NdaysAgo` — case-sensitive, like the real service."""
    text = raw.strip()
    if text == "today":
        return virtual_today()
    if text == "yesterday":
        return virtual_today() - timedelta(days=1)
    relative = _RELATIVE_PATTERN.fullmatch(text)
    if relative:
        return virtual_today() - timedelta(days=int(relative.group(1)))
    if _ISO_PATTERN.fullmatch(text):
        return date.fromisoformat(text)
    raise ValueError(raw)

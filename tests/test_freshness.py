"""The freshness model: recent days grow with the clock.

This replaces boondmanager-mock's `evolution.py`: instead of scripted
events, a per-session processing lag (`lag_hours`). What these tests prove
is exactly what the insights360 pipeline must know how to absorb —
re-extracting a recent window gives MORE rows, never fewer, and history
never moves.

Advancing the clock ALSO expires bearers (virtual 3600 s TTL): each read
therefore re-obtains its token, exactly like a real client refreshing. The
HTTP endpoint /__admin/clock is tested with the control plane; here the
clock is manipulated directly (in-process mode).
"""

from datetime import date

import ga_mock
from ga_mock.clock import clock
from ga_mock.dataset import build_day

SEED = 42
PATH = "/v1beta/properties/424242001:runReport"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

ONE_DAY = 86_400


def _sessions_for_day(client, day: date) -> int:
    token = client.post(
        "/token", data={"grant_type": GRANT, "assertion": ga_mock.build_assertion()}
    ).json()["access_token"]
    response = client.post(
        PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "dateRanges": [{"startDate": str(day), "endDate": str(day)}],
            "metrics": [{"name": "sessions"}],
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json().get("rows", [])
    return int(rows[0]["metricValues"][0]["value"]) if rows else 0


def test_today_is_partial(client):
    """At the anchor (July 15, 2:30 pm), the current day isn't done being
    processed: the report must show STRICTLY less than the world actually
    contains."""
    visible = _sessions_for_day(client, date(2026, 7, 15))
    total = len(build_day(SEED, date(2026, 7, 15)))
    assert 0 < visible < total


def test_advancing_the_clock_grows_recent_days(client):
    before_15 = _sessions_for_day(client, date(2026, 7, 15))
    before_14 = _sessions_for_day(client, date(2026, 7, 14))
    before_08 = _sessions_for_day(client, date(2026, 7, 8))

    clock.offset_seconds += 6 * 3600

    after_15 = _sessions_for_day(client, date(2026, 7, 15))
    after_14 = _sessions_for_day(client, date(2026, 7, 14))
    after_08 = _sessions_for_day(client, date(2026, 7, 8))

    assert after_15 >= before_15
    assert after_14 >= before_14
    assert after_15 + after_14 > before_15 + before_14
    # a day out of the freshness window is IMMUTABLE
    assert after_08 == before_08 == len(build_day(SEED, date(2026, 7, 8)))


def test_after_the_window_the_day_is_complete(client):
    clock.offset_seconds += 3 * ONE_DAY
    for day in (date(2026, 7, 14), date(2026, 7, 15)):
        assert _sessions_for_day(client, day) == len(build_day(SEED, day))


def test_monotonic_growth(client):
    values = [_sessions_for_day(client, date(2026, 7, 15))]
    for _ in range(3):
        clock.offset_seconds += 4 * 3600
        values.append(_sessions_for_day(client, date(2026, 7, 15)))
    assert values == sorted(values)


def test_determinism_after_reset(client):
    clock.offset_seconds += ONE_DAY
    first = _sessions_for_day(client, date(2026, 7, 15))
    ga_mock.state.reset()
    clock.offset_seconds += ONE_DAY
    second = _sessions_for_day(client, date(2026, 7, 15))
    assert first == second


def test_end_to_end_incremental_scenario(client):
    """The dlt pipeline's move: extract a window, come back the next day,
    RE-extract the same window + the new day. Recent days have grown,
    history hasn't moved, the new day exists."""
    window = [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)]
    old = date(2026, 7, 10)

    extraction_1 = {d: _sessions_for_day(client, d) for d in window}
    old_1 = _sessions_for_day(client, old)

    clock.offset_seconds += ONE_DAY

    extraction_2 = {d: _sessions_for_day(client, d) for d in window}
    assert all(extraction_2[d] >= extraction_1[d] for d in window)
    assert sum(extraction_2.values()) > sum(extraction_1.values())
    assert _sessions_for_day(client, old) == old_1
    # the world's "new day" has appeared, partial but not empty
    assert _sessions_for_day(client, date(2026, 7, 16)) > 0

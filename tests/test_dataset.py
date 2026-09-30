"""The world: determinism, volume, seasonality, campaigns, invariants."""

from datetime import date, timedelta

from ga_mock.dataset import build_day, planned_new

SEED = 42


def test_deterministic_down_to_the_byte():
    d = date(2026, 6, 3)
    assert build_day(SEED, d) == build_day(SEED, d)
    assert build_day(7, d) != build_day(SEED, d)


def test_volume_bounds():
    # first week of June 2026: no holiday, no campaign (webinar-finops
    # starts on the 8th) — the site's "ordinary" regime.
    for day in range(1, 6):
        n = len(build_day(SEED, date(2026, 6, day)))
        assert 15 <= n <= 170


def test_weekday_much_higher_than_weekend():
    monday = date(2026, 6, 1)
    weekdays = [len(build_day(SEED, monday + timedelta(days=i))) for i in range(5)]
    weekend = [len(build_day(SEED, monday + timedelta(days=i))) for i in (5, 6)]
    assert sum(weekdays) / len(weekdays) >= 3 * (sum(weekend) / len(weekend))


def test_campaign_present_and_converting():
    d = date(2026, 2, 18)  # recrutement-cyber-2026 at cruising speed
    in_campaign = [s for s in build_day(SEED, d) if s.campaign == "recrutement-cyber-2026"]
    assert in_campaign, "campaign extras must exist mid-February 2026"
    assert all(s.source == "linkedin.com" and s.medium == "cpc" for s in in_campaign)
    assert all(s.channel_group == "Paid Social" for s in in_campaign)

    applications = 0
    for j in range(14):
        for s in build_day(SEED, date(2026, 2, 9) + timedelta(days=j)):
            if s.campaign == "recrutement-cyber-2026":
                applications += dict(s.events).get("job_apply", 0)
    assert applications > 0


def test_invariants_of_a_day():
    d = date(2026, 5, 20)
    sessions = build_day(SEED, d)
    new_ones = [s for s in sessions if s.is_new]
    # the volume margin guarantees min(planned_new, organic) == planned_new
    assert len(new_ones) == planned_new(SEED, d)
    assert all(s.is_new for s in sessions[: len(new_ones)])
    assert len({s.session_id for s in sessions}) == len(sessions)
    for s in sessions:
        assert s.landing_page == s.pages[0]
        assert s.engagement_seconds <= s.duration_seconds
        key_event = any(name in ("generate_lead", "job_apply") for name, _ in s.events)
        assert s.engaged == (s.engagement_seconds >= 10 or key_event or len(s.pages) >= 2)
        assert s.user_id.split(".")[0] <= f"{d:%Y%m%d}"
        if s.is_new:
            assert s.user_id.startswith(f"{d:%Y%m%d}.")
        assert dict(s.events)["page_view"] == len(s.pages)


def test_new_visitor_ids_unique_over_30_days():
    seen: set[str] = set()
    for j in range(30):
        for s in build_day(SEED, date(2026, 4, 1) + timedelta(days=j)):
            if s.is_new:
                assert s.user_id not in seen
                seen.add(s.user_id)


def test_world_empty_before_history():
    from datetime import date as d

    assert build_day(SEED, d(2024, 12, 31)) == ()
    assert build_day(SEED, d(2020, 6, 1)) == ()
    assert len(build_day(SEED, d(2025, 1, 2))) > 0

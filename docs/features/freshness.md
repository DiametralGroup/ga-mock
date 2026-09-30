---
type: feature
description: >
  The mock's freshness model: per-session processing latency, partial and
  monotonically growing recent days, an anchored virtual clock. This is the
  lever that lets consumers test their re-extraction of the last N days.
sources_of_truth:
  - src/ga_mock/dataset/sessions.py (lag_hours)
  - src/ga_mock/state.py (visible_sessions)
  - src/ga_mock/clock.py (anchor + offset)
review_triggers:
  - a change to the default GA_MOCK_FRESHNESS_HOURS
  - a change to the time anchor
update_policy: propose
last_verified: 2026-08-02
---

# Freshness and the virtual clock

GA4 does not freeze a day at midnight: a recent day's data keeps arriving for
~48h (processing, late hits). A pipeline that only extracts "new dates"
therefore silently undercounts the most recent days. The mock reproduces this
trap so the pipeline learns to work around it.

## The model

- Each session carries `lag_hours = FRESHNESS_HOURS x u^1.6` (u drawn per
  session): most sessions are visible within a few hours, the tail stretches
  to the full window.
- A session is SERVED only if `ts + lag_hours <= virtual_now()`.
- Consequences: days out of the window are complete and immutable; today and
  yesterday grow MONOTONICALLY as the clock advances; history is never
  rewritten (`build_day` is a pure function of (seed, day)).

## The clock

`virtual_now() = ANCHOR (2026-07-15T14:30+02:00) + offset`. The base is FIXED
— not `time.time()` — because `today`/`NdaysAgo` are part of the API surface
and must fall inside the generated world, and because the anchor shares its
date with boondmanager-mock (consistent cross-source BI joins in dev).

Advance it: `POST /__admin/clock {"advance_seconds": 86400}`. Advancing ages
relative dates, bearer expiry (TTL 3600 virtual seconds — a client must renew
its token after a big jump) and freshness visibility TOGETHER.
`POST /__admin/reset` resets time back to the anchor.

## The expected consumer behavior

Re-extract a sliding window (>= 3 days for a 48h window) with a `merge` on
the grain key, never a plain "append new dates". The mock's reference test:
`tests/test_freshness.py::test_scenario_incremental_bout_en_bout`.

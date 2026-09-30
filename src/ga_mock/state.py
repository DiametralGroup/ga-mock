"""Mutable server state.

A module-level singleton: the same instance for the FastAPI app, for
`/__admin` and for in-process tests. `reset()` is the ONLY reconstruction
point — startup, admin and tests all go through it, otherwise two reset
paths would eventually diverge.
"""

from __future__ import annotations

from datetime import date, timedelta

from .clock import clock, virtual_now
from .dataset.sessions import Session, build_day
from .errors import (
    MESSAGE_429_DAY,
    MESSAGE_429_HOUR,
    MESSAGE_429_PROJECT_HOUR,
    QuotaError,
)
from .injection import engine
from .settings import settings


class MockState:
    def __init__(self) -> None:
        self.seed: int = settings.seed
        self._days: dict[date, tuple[Session, ...]] = {}
        self.quota_day_consumed: int = 0
        self.quota_hour_consumed: int = 0
        self.quota_project_hour_consumed: int = 0
        # ONE single construction path: init goes through reset(), otherwise
        # the environment's injection baseline would only apply to explicit
        # resets and never to container startup.
        self.reset()

    def consume_quota(self, tokens: int) -> None:
        """Deducts tokens — NATURAL exhaustion produces the same 429 as the
        real service. Rare with the default caps; the `quota_exhausted`
        injection forces the case without waiting.

        THREE buckets, like at the vendor: "An API request consumes a single
        number of tokens, and that number is deducted from all of the hourly,
        daily, and per project hourly quotas." The project/hour bucket (35%
        of the hourly one) is therefore the FIRST to run out at the default
        caps — a consumer watching only `tokensPerHour` will be surprised
        here rather than in prod.
        """
        if self.quota_day_consumed + tokens > settings.quota_tokens_per_day:
            raise QuotaError(MESSAGE_429_DAY)
        if self.quota_hour_consumed + tokens > settings.quota_tokens_per_hour:
            raise QuotaError(MESSAGE_429_HOUR)
        if self.quota_project_hour_consumed + tokens > settings.quota_tokens_per_project_per_hour:
            raise QuotaError(MESSAGE_429_PROJECT_HOUR)
        self.quota_day_consumed += tokens
        self.quota_hour_consumed += tokens
        self.quota_project_hour_consumed += tokens

    def day(self, d: date) -> tuple[Session, ...]:
        """Lazy materialization + cache.

        A day is a pure function of (seed, day): the cache is therefore
        invalidated ONLY by `reset()` — never by the passing of time, only
        the VISIBILITY of sessions depends on the clock.
        """
        if d not in self._days:
            self._days[d] = build_day(self.seed, d)
        return self._days[d]

    def visible_sessions(self, d: date) -> tuple[Session, ...]:
        """The "already processed" subset of the day.

        GA4 freshness model: a session only exists for the API once its
        processing latency (`lag_hours`) has elapsed. Recent days therefore
        grow monotonically as the clock advances — that's the lever that lets
        consumers test re-extracting the last N days.
        """
        limit = virtual_now()
        return tuple(s for s in self.day(d) if s.ts + timedelta(hours=s.lag_hours) <= limit)

    def days_materialized(self) -> int:
        return len(self._days)

    def reset(self, seed: int | None = None) -> None:
        """Reloads the environment then rebuilds the state.

        An explicit `seed` (from /__admin/reset) takes priority over the
        environment; without it we fall back to the deployment configuration,
        not to some remembered magic state. The virtual clock is part of the
        state: a reset also brings time back to the anchor.
        """
        settings.reload()
        self.seed = settings.seed if seed is None else seed
        self._days.clear()
        self.quota_day_consumed = 0
        self.quota_hour_consumed = 0
        self.quota_project_hour_consumed = 0
        clock.offset_seconds = 0.0
        engine.reset()


state = MockState()

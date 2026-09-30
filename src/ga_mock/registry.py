"""The registry: THE list of dimensions and metrics served.

Everything the mock knows how to do is declared here, and NOTHING that isn't
here: request validation, aggregation and the metadata endpoint all derive
from the same registry — the self-description of `GET …/metadata` is
therefore accurate by construction, never a hand-maintained document that
eventually lies.

The fan-out: `pagePath` and `eventName` are not session attributes but
sub-units (a page VIEW, an event). When requested, each session explodes into
units; session-scoped metrics stay accurate because the accumulator only
counts a session ONCE per group (identifier sets), while unit-scoped metrics
count each unit. GA4's cross-scope semantics are only approximated — recorded
in docs/UNVERIFIED-FIELDS.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from .dataset.sessions import Session

KEY_EVENTS = frozenset({"generate_lead", "job_apply"})

# Former names still announced by `metadata`, recorded against a real
# property. They are NOT accepted as input though: the field is declarative,
# and that's what the service does.
DEPRECATED_NAMES: dict[str, tuple[str, ...]] = {
    "dayOfWeek": ("dayOfWeekZero",),
    "sessionDefaultChannelGroup": ("sessionDefaultChannelGrouping",),
    "keyEvents": ("conversions",),
    "sessionKeyEventRate": ("sessionConversionRate",),
}

TYPE_INTEGER = "TYPE_INTEGER"
TYPE_FLOAT = "TYPE_FLOAT"
TYPE_SECONDS = "TYPE_SECONDS"


@dataclass(frozen=True, slots=True)
class Unit:
    """A session, possibly reduced to a page view or an event."""

    session: Session
    page: str | None = None
    event: str | None = None
    event_count: int = 1


class Accumulator:
    """The state of a result group during aggregation.

    Identifier sets are the heart of the contract: `totalUsers` is a REAL
    deduplication, not a sum — that's the whole reason for the dataset's
    session level to exist.
    """

    __slots__ = (
        "active_users",
        "duration",
        "engaged_sessions",
        "engagement",
        "events",
        "key_events",
        "new_users",
        "page_views",
        "sessions_seen",
        "sessions_with_key_event",
        "users",
    )

    def __init__(self) -> None:
        self.sessions_seen: set[str] = set()
        self.users: set[str] = set()
        self.active_users: set[str] = set()
        self.engaged_sessions = 0
        self.sessions_with_key_event = 0
        self.new_users = 0
        self.duration = 0
        self.engagement = 0
        self.page_views = 0
        self.events = 0
        self.key_events = 0

    def add(self, unit: Unit, *, fan_page: bool, fan_event: bool) -> None:
        s = unit.session
        if s.session_id not in self.sessions_seen:
            # SESSION-SCOPED contributions: only once per group, even if the
            # session fans out into several units within it.
            self.sessions_seen.add(s.session_id)
            self.users.add(s.user_id)
            if s.engaged or s.is_new:
                self.active_users.add(s.user_id)
            if s.engaged:
                self.engaged_sessions += 1
            key = sum(n for name, n in s.events if name in KEY_EVENTS)
            if key:
                self.sessions_with_key_event += 1
            if s.is_new:
                self.new_users += 1
            self.duration += s.duration_seconds
            self.engagement += s.engagement_seconds
            if not fan_page:
                self.page_views += len(s.pages)
            if not fan_event:
                self.events += sum(n for _, n in s.events)
                self.key_events += key
        if fan_page:
            self.page_views += 1
        if fan_event:
            self.events += unit.event_count
            if unit.event in KEY_EVENTS:
                self.key_events += unit.event_count


# ── Dimensions ───────────────────────────────────────────────────────────────


def _week(d: date) -> str:
    """GA week number: SUNDAY-Saturday weeks, week 01 starts on January 1st
    (partial). Edge rule unattested — UNVERIFIED."""
    jan1 = date(d.year, 1, 1)
    sunday0_jan1 = (jan1.weekday() + 1) % 7
    return f"{((d - jan1).days + sunday0_jan1) // 7 + 1:02d}"


@dataclass(frozen=True, slots=True)
class Dimension:
    api_name: str
    ui_name: str
    description: str
    category: str
    extract: Callable[[Unit], str] = field(repr=False)
    scope: str = "session"  # "session" | "page" | "event" — drives the fan-out
    # Date family: extractable from a plain calendar day, which allows the
    # keepEmptyRows "spine" without fabricating a fake session.
    from_date: Callable[[date], str] | None = field(default=None, repr=False)


def _fmt_date(d: date) -> str:
    return f"{d:%Y%m%d}"


def _fmt_month(d: date) -> str:
    return f"{d.month:02d}"


def _fmt_year_month(d: date) -> str:
    return f"{d:%Y%m}"


def _fmt_day_of_week(d: date) -> str:
    return str((d.weekday() + 1) % 7)


DIMENSIONS: dict[str, Dimension] = {
    d.api_name: d
    for d in (
        Dimension(
            "date",
            "Date",
            "The date of the session, formatted YYYYMMDD.",
            "Time",
            lambda u: _fmt_date(u.session.date),
            from_date=_fmt_date,
        ),
        Dimension(
            "week",
            "Week",
            "The week of the session: weeks start on Sunday, week 01 starts on January 1st.",
            "Time",
            lambda u: _week(u.session.date),
            from_date=_week,
        ),
        Dimension(
            "month",
            "Month",
            "The month of the session, a two digit number from 01 to 12.",
            "Time",
            lambda u: _fmt_month(u.session.date),
            from_date=_fmt_month,
        ),
        Dimension(
            "yearMonth",
            "Year month",
            "The year and month of the session, formatted YYYYMM.",
            "Time",
            lambda u: _fmt_year_month(u.session.date),
            from_date=_fmt_year_month,
        ),
        Dimension(
            "dayOfWeek",
            "Day of week",
            "The day of the week: a one digit number, starting with Sunday as 0.",
            "Time",
            lambda u: _fmt_day_of_week(u.session.date),
            from_date=_fmt_day_of_week,
        ),
        Dimension(
            "sessionDefaultChannelGroup",
            "Session default channel group",
            "The default channel group attributed to the session.",
            "Traffic Source",
            lambda u: u.session.channel_group,
        ),
        Dimension(
            "sessionSource",
            "Session source",
            "The source attributed to the session.",
            "Traffic Source",
            lambda u: u.session.source,
        ),
        Dimension(
            "sessionMedium",
            "Session medium",
            "The medium attributed to the session.",
            "Traffic Source",
            lambda u: u.session.medium,
        ),
        Dimension(
            "sessionSourceMedium",
            "Session source / medium",
            "The combined values of sessionSource and sessionMedium.",
            "Traffic Source",
            lambda u: f"{u.session.source} / {u.session.medium}",
        ),
        Dimension(
            "sessionCampaignName",
            "Session campaign",
            "The marketing campaign name attributed to the session.",
            "Traffic Source",
            lambda u: u.session.campaign,
        ),
        Dimension(
            "deviceCategory",
            "Device category",
            "The type of device: desktop, mobile or tablet.",
            "Platform / Device",
            lambda u: u.session.device_category,
        ),
        Dimension(
            "operatingSystem",
            "Operating system",
            "The operating system of the visitor's device.",
            "Platform / Device",
            lambda u: u.session.operating_system,
        ),
        Dimension(
            "browser",
            "Browser",
            "The browser used to view the website.",
            "Platform / Device",
            lambda u: u.session.browser,
        ),
        Dimension(
            "country",
            "Country",
            "The country from which the session originated.",
            "Geography",
            lambda u: u.session.country,
        ),
        Dimension(
            "region",
            "Region",
            "The geographic region from which the session originated.",
            "Geography",
            lambda u: u.session.region,
        ),
        Dimension(
            "city",
            "City",
            "The city from which the session originated.",
            "Geography",
            lambda u: u.session.city,
        ),
        Dimension(
            "landingPage",
            "Landing page",
            "The page path of the first pageview of the session.",
            "Page / Screen",
            lambda u: u.session.landing_page,
        ),
        Dimension(
            "pagePath",
            "Page path",
            "The path of the page that was viewed.",
            "Page / Screen",
            lambda u: u.page or "",
            scope="page",
        ),
        Dimension(
            "eventName",
            "Event name",
            "The name of the event.",
            "Event",
            lambda u: u.event or "",
            scope="event",
        ),
        Dimension(
            "newVsReturning",
            "New / returning",
            "Whether the session comes from a new or a returning user.",
            "User",
            lambda u: "new" if u.session.is_new else "returning",
        ),
    )
}


# ── Metrics ──────────────────────────────────────────────────────────────────


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True, slots=True)
class Metric:
    api_name: str
    ui_name: str
    description: str
    category: str
    metric_type: str
    compute: Callable[[Accumulator], float | int] = field(repr=False)


METRICS: dict[str, Metric] = {
    m.api_name: m
    for m in (
        Metric(
            "sessions",
            "Sessions",
            "The number of sessions.",
            "Session",
            TYPE_INTEGER,
            lambda a: len(a.sessions_seen),
        ),
        Metric(
            "totalUsers",
            "Total users",
            "The number of distinct users.",
            "User",
            TYPE_INTEGER,
            lambda a: len(a.users),
        ),
        Metric(
            "activeUsers",
            "Active users",
            "The number of distinct users with an engaged session or a first visit.",
            "User",
            TYPE_INTEGER,
            lambda a: len(a.active_users),
        ),
        Metric(
            "newUsers",
            "New users",
            "The number of first-time visitor sessions.",
            "User",
            TYPE_INTEGER,
            lambda a: a.new_users,
        ),
        Metric(
            "engagedSessions",
            "Engaged sessions",
            "The number of sessions that "
            "lasted 10 seconds or longer, or had a key event, or 2 or more page "
            "views.",
            "Session",
            TYPE_INTEGER,
            lambda a: a.engaged_sessions,
        ),
        Metric(
            "engagementRate",
            "Engagement rate",
            "Engaged sessions divided by sessions.",
            "Session",
            TYPE_FLOAT,
            lambda a: _ratio(a.engaged_sessions, len(a.sessions_seen)),
        ),
        Metric(
            "bounceRate",
            "Bounce rate",
            "The percentage of sessions that were not engaged: 1 minus the engagement rate.",
            "Session",
            TYPE_FLOAT,
            lambda a: (
                1.0 - _ratio(a.engaged_sessions, len(a.sessions_seen)) if a.sessions_seen else 0.0
            ),
        ),
        Metric(
            "averageSessionDuration",
            "Average session duration",
            "The mean session duration, in seconds.",
            "Session",
            TYPE_SECONDS,
            lambda a: _ratio(a.duration, len(a.sessions_seen)),
        ),
        Metric(
            "userEngagementDuration",
            "User engagement",
            "The total time the website was in the foreground, in seconds.",
            "User",
            TYPE_SECONDS,
            lambda a: a.engagement,
        ),
        Metric(
            "screenPageViews",
            "Views",
            "The number of page views.",
            "Page / Screen",
            TYPE_INTEGER,
            lambda a: a.page_views,
        ),
        Metric(
            "screenPageViewsPerSession",
            "Views per session",
            "Page views divided by sessions.",
            "Page / Screen",
            TYPE_FLOAT,
            lambda a: _ratio(a.page_views, len(a.sessions_seen)),
        ),
        Metric(
            "eventCount",
            "Event count",
            "The total number of events.",
            "Event",
            TYPE_INTEGER,
            lambda a: a.events,
        ),
        Metric(
            "keyEvents",
            "Key events",
            "The number of key events (generate_lead, job_apply).",
            "Event",
            TYPE_FLOAT,
            lambda a: float(a.key_events),
        ),
        Metric(
            "sessionKeyEventRate",
            "Session key event rate",
            "The percentage of sessions in which a key event occurred.",
            "Session",
            TYPE_FLOAT,
            lambda a: _ratio(a.sessions_with_key_event, len(a.sessions_seen)),
        ),
        Metric(
            "sessionsPerUser",
            # The UI name says what the formula ACTUALLY does: the
            # denominator is the number of ACTIVE users, not the total.
            # Recorded from the service — the mock used to divide by
            # totalUsers.
            "Sessions per active user",
            "Sessions divided by active users.",
            "Session",
            TYPE_FLOAT,
            lambda a: _ratio(len(a.sessions_seen), len(a.active_users)),
        ),
    )
}


# Comparisons shipped by default by GA4, recorded against a real property.
# They are NOT served for `properties/0`: zero describes the schema common to
# all properties, comparisons belong to a property.
STANDARD_COMPARISONS: tuple[dict[str, str], ...] = (
    {
        "apiName": "comparisons/allUsers",
        "uiName": "All Users",
        "description": "Includes all your data.",
    },
    {
        "apiName": "comparisons/directTraffic",
        "uiName": "Direct traffic",
        "description": "Sessions acquired directly.",
    },
    {
        "apiName": "comparisons/emailSmsAndPushNotificationsTraffic",
        "uiName": "Email, SMS & push notifications traffic",
        "description": "Sessions acquired via emails, SMS or push notifications.",
    },
    {
        "apiName": "comparisons/mobileTraffic",
        "uiName": "Mobile traffic",
        "description": "Traffic on mobile phones.",
    },
    {
        "apiName": "comparisons/organicTraffic",
        "uiName": "Organic traffic",
        "description": "Sessions acquired via organic channels.",
    },
    {
        "apiName": "comparisons/paidTraffic",
        "uiName": "Paid traffic",
        "description": "Sessions acquired via paid channels.",
    },
    {
        "apiName": "comparisons/referralAndAffiliatesTraffic",
        "uiName": "Referral & affiliates traffic",
        "description": "Sessions acquired via referrals or affiliates.",
    },
    {
        "apiName": "comparisons/tabletTraffic",
        "uiName": "Tablet traffic",
        "description": "Traffic on tablets.",
    },
    {
        "apiName": "comparisons/webTraffic",
        "uiName": "Web traffic",
        "description": "Traffic on desktops.",
    },
)


def _metadata_entry(api_name: str, base: dict[str, object]) -> dict[str, object]:
    """A `metadata` entry, down to the proto3 omissions.

    `customDefinition` is ABSENT when false — the service never returns it
    for standard fields, and the mock used to write it unconditionally.
    """
    if api_name in DEPRECATED_NAMES:
        base["deprecatedApiNames"] = list(DEPRECATED_NAMES[api_name])
    return base


def metadata_payload(property_id: str) -> dict[str, object]:
    """The body of `GET /v1beta/properties/{id}/metadata` — derived from the registry."""
    body: dict[str, object] = {
        "name": f"properties/{property_id}/metadata",
        "dimensions": [
            _metadata_entry(
                d.api_name,
                {
                    "apiName": d.api_name,
                    "uiName": d.ui_name,
                    "description": d.description,
                    "category": d.category,
                },
            )
            for d in DIMENSIONS.values()
        ],
        "metrics": [
            _metadata_entry(
                m.api_name,
                {
                    "apiName": m.api_name,
                    "uiName": m.ui_name,
                    "description": m.description,
                    "category": m.category,
                    "type": m.metric_type,
                },
            )
            for m in METRICS.values()
        ],
    }
    if property_id != "0":
        body["comparisons"] = [dict(c) for c in STANDARD_COMPARISONS]
    return body

"""The sessions of the Boréal Conseil showcase site — the mock's world.

Same universe as boondmanager-mock (the fictional 34-person ESN: Paris/Lyon/
Nantes agencies, Data & AI / Cloud & Platforms / Cybersecurity / Digital
Transformation business units): web traffic tells the SAME company story as
the CRM, so that cross-source BI joins line up in dev.

Determinism, the rule that governs the whole file:

  `build_day(seed, d)` depends ONLY on (seed, d) — `random.Random(f"{seed}:{d}")`,
  never `datetime.now()`, never another day's state. Extending the timeline
  (advancing the virtual clock) therefore has NO way to rewrite history, and
  each day can be lazily materialized then cached.

The level of detail is the INDIVIDUAL session, not pre-aggregated reports:
that's what makes `totalUsers` exact (real deduplication of visitor
identifiers over any range) instead of an additive approximation that would
lie exactly where GA4 is tricky.

Visitor identities without cross-day generation: `planned_new(seed, B)` is a
pure function that fixes how many identifiers are BORN on day B. A day D
picks its returning visitors by drawing (B <= D, j < planned_new(B)) — hence
stable ids `YYYYMMDD.1jjjjj` without ever materializing another day. A draw
where B == D is a same-day case (second visit on the day of the first): rare
and plausible, since intra-day ordering isn't modeled.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..clock import paris_timezone
from ..settings import settings

HISTORY_START = date(2025, 1, 1)

# ── Volume ───────────────────────────────────────────────────────────────────

_BASE_ORGANIC = 62.0
_ANNUAL_GROWTH = 1.18

# per-day-of-week factor (Monday=0) — a B2B site lives on office hours
_DAY_OF_WEEK_FACTOR = (1.05, 1.12, 1.10, 1.08, 0.92, 0.25, 0.18)

_HOLIDAYS: frozenset[date] = frozenset(
    (
        # 2025
        date(2025, 1, 1),
        date(2025, 4, 21),
        date(2025, 5, 1),
        date(2025, 5, 8),
        date(2025, 5, 29),
        date(2025, 6, 9),
        date(2025, 7, 14),
        date(2025, 8, 15),
        date(2025, 11, 1),
        date(2025, 11, 11),
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 8),
        date(2026, 5, 14),
        date(2026, 5, 25),
        date(2026, 7, 14),
        date(2026, 8, 15),
        date(2026, 11, 1),
        date(2026, 11, 11),
        date(2026, 12, 25),
    )
)


def _season(d: date) -> float:
    """Holidays, bridge days, summer, end-of-year: the troughs of a French B2B site."""
    if d in _HOLIDAYS:
        return 0.30
    # bridge day: Friday after a Thursday holiday, Monday before a Tuesday holiday
    if d.weekday() == 4 and (d - timedelta(days=1)) in _HOLIDAYS:
        return 0.60
    if d.weekday() == 0 and (d + timedelta(days=1)) in _HOLIDAYS:
        return 0.60
    if d.month == 8 and d.day <= 24:
        return 0.55
    if (d.month == 12 and d.day >= 22) or (d.month == 1 and d.day <= 2):
        return 0.50
    return 1.0


def _organic_volume(d: date) -> float:
    """Closed form, no randomness: the same value regardless of the caller."""
    growth: float = _ANNUAL_GROWTH ** ((d - HISTORY_START).days / 365.0)
    return _BASE_ORGANIC * growth * _DAY_OF_WEEK_FACTOR[d.weekday()] * _season(d)


def planned_new(seed: int, d: date) -> int:
    """Number of visitor identifiers that are BORN on day d.

    A pure function of (seed, d), with its own generator: it's called by
    other days (picking returning visitors) and must therefore not depend on
    `build_day`'s rng consumption order. The share stays under 0.65 *
    organic when total volume exceeds 0.85 * organic: a new visitor ALWAYS
    has a session on the day they're born.
    """
    rng = random.Random(f"{seed}:new:{d.isoformat()}")
    share = 0.50 + 0.15 * rng.random()
    return max(1, round(_organic_volume(d) * share))


# ── Campaigns ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Campaign:
    name: str
    start: date
    end: date
    channel: str
    source: str
    medium: str
    extra_per_day: int
    landings: tuple[str, ...]
    mult_lead: float = 1.0
    mult_job: float = 1.0


# The calendar tells the ESN's story: hiring (the ads from the boondmanager
# world), webinars, trade shows, whitepapers. The multipliers create the
# expected correlation: campaign traffic converts more.
CAMPAIGNS: tuple[Campaign, ...] = (
    Campaign(
        "recrutement-data-2025",
        date(2025, 3, 3),
        date(2025, 4, 13),
        "Paid Social",
        "linkedin.com",
        "cpc",
        14,
        (
            "/offres-emploi/consultant-data-engineer-paris",
            "/offres-emploi/chef-de-projet-data-nantes",
        ),
        mult_job=3.0,
    ),
    Campaign(
        "webinar-mlops-printemps-2025",
        date(2025, 4, 22),
        date(2025, 5, 16),
        "Paid Social",
        "linkedin.com",
        "cpc",
        10,
        ("/blog/socle-mlops-industrialiser",),
        mult_lead=2.0,
    ),
    Campaign(
        "livre-blanc-data-mesh",
        date(2025, 9, 15),
        date(2025, 10, 31),
        "Paid Search",
        "google",
        "cpc",
        12,
        ("/expertises/data-ia", "/blog/data-mesh-retours-terrain"),
        mult_lead=2.5,
    ),
    Campaign(
        "voeux-2026",
        date(2026, 1, 6),
        date(2026, 1, 12),
        "Email",
        "newsletter",
        "email",
        18,
        ("/", "/realisations"),
        mult_lead=0.5,
    ),
    Campaign(
        "recrutement-cyber-2026",
        date(2026, 2, 2),
        date(2026, 3, 15),
        "Paid Social",
        "linkedin.com",
        "cpc",
        15,
        ("/offres-emploi/consultant-cybersecurite-paris",),
        mult_job=3.0,
    ),
    Campaign(
        "salon-big-data-paris-2026",
        date(2026, 3, 9),
        date(2026, 3, 27),
        "Paid Search",
        "google",
        "cpc",
        13,
        ("/realisations", "/expertises/data-ia"),
        mult_lead=2.0,
    ),
    Campaign(
        "webinar-finops-2026",
        date(2026, 6, 8),
        date(2026, 6, 26),
        "Paid Social",
        "linkedin.com",
        "cpc",
        11,
        ("/blog/finops-maitriser-couts-cloud",),
        mult_lead=2.0,
    ),
)


def _ramp(c: Campaign, d: date) -> float:
    """Ramp up/down over 5 days rather than a rectangular window."""
    if not (c.start <= d <= c.end):
        return 0.0
    return min(1.0, ((d - c.start).days + 1) / 5.0, ((c.end - d).days + 1) / 5.0)


# ── Site tree (~32 pages, aligned with the boondmanager world) ─────────────

HOME_PAGE = "/"
EXPERTISE_PAGES = (
    "/expertises/data-ia",
    "/expertises/cloud-plateformes",
    "/expertises/cybersecurite",
    "/expertises/transformation-digitale",
)
CASE_STUDY_PAGES = (
    "/realisations/refonte-plateforme-data-retail",
    "/realisations/migration-cloud-banque",
    "/realisations/socle-mlops-energie",
    "/realisations/tableau-de-bord-pilotage-finance",
    "/realisations/data-mesh-domaine-client",
    "/realisations/audit-securite-industrie",
)
BLOG_PAGES = (
    "/blog/socle-mlops-industrialiser",
    "/blog/finops-maitriser-couts-cloud",
    "/blog/data-mesh-retours-terrain",
    "/blog/dbt-tests-qualite-donnees",
    "/blog/rgpd-anonymisation-datalake",
    "/blog/kubernetes-securite-tenants",
    "/blog/llm-entreprise-cas-usage",
    "/blog/observabilite-data-pipelines",
    "/blog/recrutement-data-engineers-2026",
    "/blog/migration-postgres-zero-downtime",
)
JOB_PAGES = (
    "/offres-emploi/consultant-data-engineer-paris",
    "/offres-emploi/consultant-data-scientist-lyon",
    "/offres-emploi/ingenieur-devops-sre-paris",
    "/offres-emploi/consultant-cybersecurite-paris",
    "/offres-emploi/chef-de-projet-data-nantes",
    "/offres-emploi/consultant-cloud-finops-lyon",
    "/offres-emploi/developpeur-fullstack-data-nantes",
    "/offres-emploi/alternant-data-analyst-paris",
)
PAGE_CONTACT = "/contact"
ABOUT_PAGE = "/a-propos"
_EXPERTISE_INDEX = "/expertises"
_CASE_STUDY_INDEX = "/realisations"
_BLOG_INDEX = "/blog"
_JOB_INDEX = "/offres-emploi"

ALL_PAGES: tuple[str, ...] = (
    HOME_PAGE,
    _EXPERTISE_INDEX,
    *EXPERTISE_PAGES,
    _CASE_STUDY_INDEX,
    *CASE_STUDY_PAGES,
    _BLOG_INDEX,
    *BLOG_PAGES,
    _JOB_INDEX,
    *JOB_PAGES,
    PAGE_CONTACT,
    ABOUT_PAGE,
)


def _navigation_graph() -> dict[str, tuple[str, ...]]:
    """Navigation graph: from each page, the plausible follow-ups.

    /contact is terminal — you don't navigate away from a submitted form.
    """
    graph: dict[str, tuple[str, ...]] = {
        HOME_PAGE: (
            _EXPERTISE_INDEX,
            _CASE_STUDY_INDEX,
            _BLOG_INDEX,
            _JOB_INDEX,
            ABOUT_PAGE,
            PAGE_CONTACT,
        ),
        _EXPERTISE_INDEX: EXPERTISE_PAGES,
        _CASE_STUDY_INDEX: CASE_STUDY_PAGES,
        _BLOG_INDEX: BLOG_PAGES,
        _JOB_INDEX: JOB_PAGES,
        PAGE_CONTACT: (),
        ABOUT_PAGE: (PAGE_CONTACT, _EXPERTISE_INDEX, HOME_PAGE),
    }
    for page in EXPERTISE_PAGES:
        others = tuple(p for p in EXPERTISE_PAGES if p != page)
        graph[page] = (_CASE_STUDY_INDEX, PAGE_CONTACT, *others)
    for page in CASE_STUDY_PAGES:
        others = tuple(p for p in CASE_STUDY_PAGES if p != page)
        graph[page] = (*EXPERTISE_PAGES[:2], PAGE_CONTACT, *others[:2])
    for page in BLOG_PAGES:
        others = tuple(p for p in BLOG_PAGES if p != page)
        graph[page] = (*others[:3], EXPERTISE_PAGES[0], PAGE_CONTACT, _BLOG_INDEX)
    for page in JOB_PAGES:
        others = tuple(p for p in JOB_PAGES if p != page)
        graph[page] = (*others[:2], PAGE_CONTACT, _JOB_INDEX)
    return graph


_NAVIGATION_GRAPH = _navigation_graph()

# ── Organic acquisition mix, devices, geography ──────────────────────────────

# (channel, source, medium) — paid channels only exist THROUGH campaigns: a
# 34-person ESN has no "always-on" cpc.
_ORGANIC_MIX: tuple[tuple[tuple[str, str, str], int], ...] = (
    (("Organic Search", "google", "organic"), 33),
    (("Organic Search", "bing", "organic"), 4),
    (("Direct", "(direct)", "(none)"), 22),
    (("Referral", "welcometothejungle.com", "referral"), 5),
    (("Referral", "societe.com", "referral"), 3),
    (("Referral", "glassdoor.fr", "referral"), 3),
    (("Organic Social", "linkedin.com", "social"), 9),
    (("Organic Social", "x.com", "social"), 2),
    (("Email", "newsletter", "email"), 8),
    (("Unassigned", "(not set)", "(not set)"), 1),
)

# sessionCampaignName fallback per organic channel — unattested GA4 wording,
# recorded in docs/UNVERIFIED-FIELDS.md. Email is a real campaign name
# (monthly newsletter), computed separately.
_CAMPAIGN_BY_CHANNEL = {
    "Organic Search": "(organic)",
    "Organic Social": "(organic)",
    "Direct": "(direct)",
    "Referral": "(referral)",
    "Unassigned": "(not set)",
}

_DEVICE_MIX: tuple[tuple[str, int], ...] = (("desktop", 62), ("mobile", 33), ("tablet", 5))

_OS_BY_DEVICE: dict[str, tuple[tuple[str, int], ...]] = {
    "desktop": (("Windows", 58), ("Macintosh", 30), ("Linux", 9), ("Chrome OS", 3)),
    "mobile": (("Android", 58), ("iOS", 42)),
    "tablet": (("iOS", 70), ("Android", 30)),
}

_BROWSERS_BY_OS: dict[str, tuple[tuple[str, int], ...]] = {
    "Windows": (("Chrome", 60), ("Edge", 28), ("Firefox", 12)),
    "Macintosh": (("Chrome", 45), ("Safari", 45), ("Firefox", 10)),
    "Linux": (("Firefox", 50), ("Chrome", 50)),
    "Chrome OS": (("Chrome", 100),),
    "Android": (("Chrome", 75), ("Samsung Internet", 25)),
    "iOS": (("Safari", 85), ("Chrome", 15)),
}

# (country, region, city) — English geographic names without accents, as
# returned by the GA API (accent policy unattested: UNVERIFIED-FIELDS.md).
_GEO_MIX: tuple[tuple[tuple[str, str, str], int], ...] = (
    (("France", "Ile-de-France", "Paris"), 38),
    (("France", "Ile-de-France", "Boulogne-Billancourt"), 4),
    (("France", "Auvergne-Rhone-Alpes", "Lyon"), 16),
    (("France", "Pays de la Loire", "Nantes"), 10),
    (("France", "Hauts-de-France", "Lille"), 5),
    (("France", "Nouvelle-Aquitaine", "Bordeaux"), 5),
    (("France", "Occitanie", "Toulouse"), 4),
    (("France", "Provence-Alpes-Cote d'Azur", "Marseille"), 3),
    (("Belgium", "Brussels", "Brussels"), 5),
    (("Switzerland", "Geneva", "Geneva"), 3),
    (("Germany", "Berlin", "Berlin"), 2),
    (("United States", "New York", "New York"), 3),
)

# landing page by channel: weights over page families
_LANDING_BY_CHANNEL: dict[str, tuple[tuple[tuple[str, ...], int], ...]] = {
    "Organic Search": (
        (BLOG_PAGES, 45),
        (EXPERTISE_PAGES, 25),
        ((HOME_PAGE,), 10),
        (CASE_STUDY_PAGES, 12),
        (JOB_PAGES, 8),
    ),
    "Direct": (
        ((HOME_PAGE,), 55),
        ((_JOB_INDEX, *JOB_PAGES), 20),
        (EXPERTISE_PAGES, 12),
        ((PAGE_CONTACT,), 5),
        ((ABOUT_PAGE,), 8),
    ),
    "Referral": (
        (CASE_STUDY_PAGES, 30),
        ((HOME_PAGE,), 30),
        ((_JOB_INDEX, *JOB_PAGES), 25),
        (BLOG_PAGES, 15),
    ),
    "Organic Social": (
        (BLOG_PAGES, 45),
        ((_JOB_INDEX, *JOB_PAGES), 30),
        ((HOME_PAGE,), 15),
        (CASE_STUDY_PAGES, 10),
    ),
    "Email": (
        (BLOG_PAGES, 55),
        (CASE_STUDY_PAGES, 20),
        ((HOME_PAGE,), 15),
        (EXPERTISE_PAGES, 10),
    ),
    "Unassigned": (((HOME_PAGE,), 100),),
}

# 0 to 5 follow-up pages; ~52% single-page sessions for a realistic bounce
# rate (30-40%) once the GA4 engagement rule is applied.
_FOLLOWUP_COUNT_WEIGHTS = (52, 22, 12, 8, 4, 2)

_HOUR_WEIGHTS = (
    1,
    1,
    1,
    1,
    1,
    2,
    4,
    8,
    14,
    20,
    22,
    20,  # 0h → 11h
    12,
    16,
    20,
    21,
    19,
    16,
    10,
    6,
    4,
    3,
    2,
    1,  # 12h → 23h
)


# ── The session ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Session:
    date: date
    ts: datetime
    session_id: str
    user_id: str
    is_new: bool
    channel_group: str
    source: str
    medium: str
    campaign: str
    device_category: str
    browser: str
    operating_system: str
    country: str
    region: str
    city: str
    landing_page: str
    pages: tuple[str, ...]
    events: tuple[tuple[str, int], ...]
    duration_seconds: int
    engagement_seconds: int
    engaged: bool
    lag_hours: float


def _weighted_choice[T](rng: random.Random, mix: tuple[tuple[T, int], ...]) -> T:
    return rng.choices([v for v, _ in mix], weights=[p for _, p in mix], k=1)[0]


def _user_id(birth: date, index: int) -> str:
    """GA "client id" shape: YYYYMMDD.1jjjjj — stable across days."""
    return f"{birth:%Y%m%d}.1{index:05d}"


def _visitor_birth(rng: random.Random, d: date, seed: int) -> tuple[date, int]:
    """Birth day (exponential recency bias) + visitor index."""
    offset = int(rng.expovariate(1 / 75.0)) + 1
    birth = max(HISTORY_START, d - timedelta(days=offset))
    return birth, rng.randrange(planned_new(seed, birth))


def _browse(rng: random.Random, landing: str) -> tuple[str, ...]:
    pages = [landing]
    followups = _weighted_choice(rng, tuple(zip(range(6), _FOLLOWUP_COUNT_WEIGHTS, strict=True)))
    for _ in range(followups):
        candidates = _NAVIGATION_GRAPH.get(pages[-1], ())
        if not candidates:
            break
        pages.append(rng.choice(candidates))
    return tuple(pages)


def _timestamp(rng: random.Random, d: date) -> datetime:
    hour = _weighted_choice(rng, tuple(zip(range(24), _HOUR_WEIGHTS, strict=True)))
    naive = datetime(d.year, d.month, d.day, hour, rng.randrange(60), rng.randrange(60))
    return naive.replace(tzinfo=paris_timezone(naive))


def _acquisition(
    rng: random.Random, d: date, campaign: Campaign | None
) -> tuple[str, str, str, str, str]:
    """(channel, source, medium, campaign name, landing page)."""
    if campaign is not None:
        return (
            campaign.channel,
            campaign.source,
            campaign.medium,
            campaign.name,
            rng.choice(campaign.landings),
        )
    channel, source, medium = _weighted_choice(rng, _ORGANIC_MIX)
    name = f"newsletter-{d:%Y-%m}" if channel == "Email" else _CAMPAIGN_BY_CHANNEL[channel]
    landing = rng.choice(_weighted_choice(rng, _LANDING_BY_CHANNEL[channel]))
    return channel, source, medium, name, landing


def _events(
    rng: random.Random,
    pages: tuple[str, ...],
    *,
    is_new: bool,
    engaged: bool,
    lead: bool,
    job_application: bool,
) -> tuple[tuple[str, int], ...]:
    events: list[tuple[str, int]] = [("session_start", 1), ("page_view", len(pages))]
    if is_new:
        events.append(("first_visit", 1))
    if engaged:
        events.append(("user_engagement", 1))
    scrolls = sum(1 for _ in pages if rng.random() < 0.45)
    if scrolls:
        events.append(("scroll", scrolls))
    if lead:
        events.append(("generate_lead", 1))
    if job_application:
        events.append(("job_apply", 1))
    return tuple(events)


def _build_session(
    rng: random.Random,
    seed: int,
    d: date,
    *,
    index: int,
    new_count: int,
    campaign: Campaign | None,
) -> Session:
    if index < new_count:
        is_new, user_id = True, _user_id(d, index)
    else:
        is_new = False
        birth, j = _visitor_birth(rng, d, seed)
        user_id = _user_id(birth, j)

    channel, source, medium, campaign_name, landing = _acquisition(rng, d, campaign)
    device = _weighted_choice(rng, _DEVICE_MIX)
    os_ = _weighted_choice(rng, _OS_BY_DEVICE[device])
    browser = _weighted_choice(rng, _BROWSERS_BY_OS[os_])
    country, region, city = _weighted_choice(rng, _GEO_MIX)
    pages = _browse(rng, landing)

    mult_lead = campaign.mult_lead if campaign else 1.0
    mult_job = campaign.mult_job if campaign else 1.0
    p_lead = 0.18 * mult_lead * (1.15 if device == "desktop" else 1.0)
    if not is_new:
        p_lead *= 1.2
    lead = PAGE_CONTACT in pages and rng.random() < min(p_lead, 0.9)
    on_job_page = any(p.startswith("/offres-emploi/") for p in pages)
    job_application = on_job_page and rng.random() < min(0.06 * mult_job, 0.9)

    duration = (
        rng.randint(2, 25)
        if len(pages) == 1
        else 25 + 45 * (len(pages) - 1) + int(rng.expovariate(1 / 30.0))
    )
    engagement = min(int(duration * (0.6 + 0.3 * rng.random())), duration)
    engaged = engagement >= 10 or lead or job_application or len(pages) >= 2

    events = _events(
        rng, pages, is_new=is_new, engaged=engaged, lead=lead, job_application=job_application
    )
    return Session(
        date=d,
        ts=_timestamp(rng, d),
        session_id=f"{d:%Y%m%d}.{index}",
        user_id=user_id,
        is_new=is_new,
        channel_group=channel,
        source=source,
        medium=medium,
        campaign=campaign_name,
        device_category=device,
        browser=browser,
        operating_system=os_,
        country=country,
        region=region,
        city=city,
        landing_page=landing,
        pages=pages,
        events=events,
        duration_seconds=duration,
        engagement_seconds=engagement,
        engaged=engaged,
        lag_hours=settings.freshness_hours * (rng.random() ** 1.6),
    )


def build_day(seed: int, d: date) -> tuple[Session, ...]:
    """All the sessions for day d — the pure function at the mock's core.

    Construction order: organic volume, campaign extras, then each session in
    a FIXED order (new visitors first). Don't reorder anything without a
    reason: any change to the rng consumption order changes the world, hence
    the de facto contract of consumer tests.
    """
    # Before the history start, the site doesn't exist: an EMPTY world, not
    # extrapolated. (Future days need no guard: their sessions are invisible
    # by construction, their timestamp is past the clock.)
    if d < HISTORY_START:
        return ()
    rng = random.Random(f"{seed}:{d.isoformat()}")
    organic = round(_organic_volume(d) * (0.85 + 0.30 * rng.random()))
    campaign_volumes: list[tuple[Campaign, int]] = []
    for c in CAMPAIGNS:
        ramp = _ramp(c, d)
        if ramp > 0:
            extra = round(c.extra_per_day * ramp * (0.7 + 0.6 * rng.random()))
            if extra:
                campaign_volumes.append((c, extra))

    new_count = min(planned_new(seed, d), organic)
    sessions: list[Session] = []
    for i in range(organic):
        sessions.append(_build_session(rng, seed, d, index=i, new_count=new_count, campaign=None))
    index = organic
    for c, extra in campaign_volumes:
        for _ in range(extra):
            sessions.append(
                _build_session(rng, seed, d, index=index, new_count=new_count, campaign=c)
            )
            index += 1
    return tuple(sessions)

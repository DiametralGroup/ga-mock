"""Le registre : LA liste des dimensions et métriques servies.

Tout ce que le mock sait faire est déclaré ici, et RIEN que ce qui est ici :
la validation des requêtes, l'agrégation et le endpoint metadata dérivent du
même registre — l'auto-description de `GET …/metadata` est donc exacte par
construction, jamais un document entretenu à la main qui finirait par mentir.

Le fan-out : `pagePath` et `eventName` ne sont pas des attributs de session
mais des sous-unités (une page VUE, un événement). Quand ils sont demandés,
chaque session éclate en unités ; les métriques de portée session restent
exactes parce que l'accumulateur ne compte une session qu'UNE fois par groupe
(ensembles d'identifiants), pendant que les métriques de portée unité comptent
chaque unité. La sémantique inter-portées de GA4 n'est qu'approchée — consigné
dans docs/UNVERIFIED-FIELDS.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from .dataset.sessions import Session

EVENEMENTS_CLES = frozenset({"generate_lead", "job_apply"})

# Anciens noms encore annoncés par `metadata`, relevés sur une vraie propriété.
# Ils ne sont pas ACCEPTÉS en entrée pour autant : le champ est déclaratif, et
# c'est ce que fait le service.
NOMS_DEPRECIES: dict[str, tuple[str, ...]] = {
    "dayOfWeek": ("dayOfWeekZero",),
    "sessionDefaultChannelGroup": ("sessionDefaultChannelGrouping",),
    "keyEvents": ("conversions",),
    "sessionKeyEventRate": ("sessionConversionRate",),
}

TYPE_INTEGER = "TYPE_INTEGER"
TYPE_FLOAT = "TYPE_FLOAT"
TYPE_SECONDS = "TYPE_SECONDS"


@dataclass(frozen=True, slots=True)
class Unite:
    """Une session, éventuellement réduite à une page vue ou un événement."""

    session: Session
    page: str | None = None
    evenement: str | None = None
    n_evenements: int = 1


class Accumulateur:
    """L'état d'un groupe de résultat pendant l'agrégation.

    Les ensembles d'identifiants sont le cœur du contrat : `totalUsers` est un
    dédoublonnage RÉEL, pas une somme — c'est toute la raison d'être du niveau
    session du dataset.
    """

    __slots__ = (
        "duree",
        "engagement",
        "evenements",
        "evenements_cles",
        "nouveaux",
        "pages_vues",
        "sessions_avec_cle",
        "sessions_engagees",
        "sessions_vues",
        "users",
        "users_actifs",
    )

    def __init__(self) -> None:
        self.sessions_vues: set[str] = set()
        self.users: set[str] = set()
        self.users_actifs: set[str] = set()
        self.sessions_engagees = 0
        self.sessions_avec_cle = 0
        self.nouveaux = 0
        self.duree = 0
        self.engagement = 0
        self.pages_vues = 0
        self.evenements = 0
        self.evenements_cles = 0

    def ajouter(self, unite: Unite, *, fan_page: bool, fan_event: bool) -> None:
        s = unite.session
        if s.session_id not in self.sessions_vues:
            # Contributions de PORTÉE SESSION : une seule fois par groupe,
            # même si la session y éclate en plusieurs unités.
            self.sessions_vues.add(s.session_id)
            self.users.add(s.user_id)
            if s.engaged or s.is_new:
                self.users_actifs.add(s.user_id)
            if s.engaged:
                self.sessions_engagees += 1
            cles = sum(n for nom, n in s.events if nom in EVENEMENTS_CLES)
            if cles:
                self.sessions_avec_cle += 1
            if s.is_new:
                self.nouveaux += 1
            self.duree += s.duration_seconds
            self.engagement += s.engagement_seconds
            if not fan_page:
                self.pages_vues += len(s.pages)
            if not fan_event:
                self.evenements += sum(n for _, n in s.events)
                self.evenements_cles += cles
        if fan_page:
            self.pages_vues += 1
        if fan_event:
            self.evenements += unite.n_evenements
            if unite.evenement in EVENEMENTS_CLES:
                self.evenements_cles += unite.n_evenements


# ── Dimensions ───────────────────────────────────────────────────────────────


def _semaine(d: date) -> str:
    """Numéro de semaine GA : semaines DIMANCHE-samedi, la semaine 01 commence
    le 1er janvier (partielle). Règle de bord non attestée — UNVERIFIED."""
    jan1 = date(d.year, 1, 1)
    dimanche0_jan1 = (jan1.weekday() + 1) % 7
    return f"{((d - jan1).days + dimanche0_jan1) // 7 + 1:02d}"


@dataclass(frozen=True, slots=True)
class Dimension:
    api_name: str
    ui_name: str
    description: str
    category: str
    extraire: Callable[[Unite], str] = field(repr=False)
    portee: str = "session"  # "session" | "page" | "event" — pilote le fan-out
    # Famille date : extraction possible depuis un simple jour calendaire, ce
    # qui permet la « spine » keepEmptyRows sans fabriquer de fausse session.
    depuis_date: Callable[[date], str] | None = field(default=None, repr=False)


def _fmt_date(d: date) -> str:
    return f"{d:%Y%m%d}"


def _fmt_mois(d: date) -> str:
    return f"{d.month:02d}"


def _fmt_annee_mois(d: date) -> str:
    return f"{d:%Y%m}"


def _fmt_jour_semaine(d: date) -> str:
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
            depuis_date=_fmt_date,
        ),
        Dimension(
            "week",
            "Week",
            "The week of the session: weeks start on Sunday, week 01 starts on January 1st.",
            "Time",
            lambda u: _semaine(u.session.date),
            depuis_date=_semaine,
        ),
        Dimension(
            "month",
            "Month",
            "The month of the session, a two digit number from 01 to 12.",
            "Time",
            lambda u: _fmt_mois(u.session.date),
            depuis_date=_fmt_mois,
        ),
        Dimension(
            "yearMonth",
            "Year month",
            "The year and month of the session, formatted YYYYMM.",
            "Time",
            lambda u: _fmt_annee_mois(u.session.date),
            depuis_date=_fmt_annee_mois,
        ),
        Dimension(
            "dayOfWeek",
            "Day of week",
            "The day of the week: a one digit number, starting with Sunday as 0.",
            "Time",
            lambda u: _fmt_jour_semaine(u.session.date),
            depuis_date=_fmt_jour_semaine,
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
            portee="page",
        ),
        Dimension(
            "eventName",
            "Event name",
            "The name of the event.",
            "Event",
            lambda u: u.evenement or "",
            portee="event",
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


# ── Métriques ────────────────────────────────────────────────────────────────


def _ratio(numerateur: float, denominateur: float) -> float:
    return numerateur / denominateur if denominateur else 0.0


@dataclass(frozen=True, slots=True)
class Metrique:
    api_name: str
    ui_name: str
    description: str
    category: str
    type_metrique: str
    calculer: Callable[[Accumulateur], float | int] = field(repr=False)


METRIQUES: dict[str, Metrique] = {
    m.api_name: m
    for m in (
        Metrique(
            "sessions",
            "Sessions",
            "The number of sessions.",
            "Session",
            TYPE_INTEGER,
            lambda a: len(a.sessions_vues),
        ),
        Metrique(
            "totalUsers",
            "Total users",
            "The number of distinct users.",
            "User",
            TYPE_INTEGER,
            lambda a: len(a.users),
        ),
        Metrique(
            "activeUsers",
            "Active users",
            "The number of distinct users with an engaged session or a first visit.",
            "User",
            TYPE_INTEGER,
            lambda a: len(a.users_actifs),
        ),
        Metrique(
            "newUsers",
            "New users",
            "The number of first-time visitor sessions.",
            "User",
            TYPE_INTEGER,
            lambda a: a.nouveaux,
        ),
        Metrique(
            "engagedSessions",
            "Engaged sessions",
            "The number of sessions that "
            "lasted 10 seconds or longer, or had a key event, or 2 or more page "
            "views.",
            "Session",
            TYPE_INTEGER,
            lambda a: a.sessions_engagees,
        ),
        Metrique(
            "engagementRate",
            "Engagement rate",
            "Engaged sessions divided by sessions.",
            "Session",
            TYPE_FLOAT,
            lambda a: _ratio(a.sessions_engagees, len(a.sessions_vues)),
        ),
        Metrique(
            "bounceRate",
            "Bounce rate",
            "The percentage of sessions that were not engaged: 1 minus the engagement rate.",
            "Session",
            TYPE_FLOAT,
            lambda a: (
                1.0 - _ratio(a.sessions_engagees, len(a.sessions_vues)) if a.sessions_vues else 0.0
            ),
        ),
        Metrique(
            "averageSessionDuration",
            "Average session duration",
            "The mean session duration, in seconds.",
            "Session",
            TYPE_SECONDS,
            lambda a: _ratio(a.duree, len(a.sessions_vues)),
        ),
        Metrique(
            "userEngagementDuration",
            "User engagement",
            "The total time the website was in the foreground, in seconds.",
            "User",
            TYPE_SECONDS,
            lambda a: a.engagement,
        ),
        Metrique(
            "screenPageViews",
            "Views",
            "The number of page views.",
            "Page / Screen",
            TYPE_INTEGER,
            lambda a: a.pages_vues,
        ),
        Metrique(
            "screenPageViewsPerSession",
            "Views per session",
            "Page views divided by sessions.",
            "Page / Screen",
            TYPE_FLOAT,
            lambda a: _ratio(a.pages_vues, len(a.sessions_vues)),
        ),
        Metrique(
            "eventCount",
            "Event count",
            "The total number of events.",
            "Event",
            TYPE_INTEGER,
            lambda a: a.evenements,
        ),
        Metrique(
            "keyEvents",
            "Key events",
            "The number of key events (generate_lead, job_apply).",
            "Event",
            TYPE_FLOAT,
            lambda a: float(a.evenements_cles),
        ),
        Metrique(
            "sessionKeyEventRate",
            "Session key event rate",
            "The percentage of sessions in which a key event occurred.",
            "Session",
            TYPE_FLOAT,
            lambda a: _ratio(a.sessions_avec_cle, len(a.sessions_vues)),
        ),
        Metrique(
            "sessionsPerUser",
            # Le nom d'interface dit ce que la formule fait VRAIMENT : le
            # dénominateur est le nombre d'utilisateurs ACTIFS, pas le total.
            # Relevé sur le service — le mock divisait par totalUsers.
            "Sessions per active user",
            "Sessions divided by active users.",
            "Session",
            TYPE_FLOAT,
            lambda a: _ratio(len(a.sessions_vues), len(a.users_actifs)),
        ),
    )
}


# Les comparaisons livrées d'office par GA4, relevées sur une vraie propriété.
# Elles ne sont PAS servies pour `properties/0` : le zéro décrit le schéma
# commun, les comparaisons appartiennent à une propriété.
COMPARAISONS_STANDARD: tuple[dict[str, str], ...] = (
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


def _entree_metadata(api_name: str, base: dict[str, object]) -> dict[str, object]:
    """Une entrée de `metadata`, aux omissions proto3 près.

    `customDefinition` est ABSENT quand il vaut false — le service ne le rend
    jamais pour les champs standard, et le mock l'écrivait systématiquement.
    """
    if api_name in NOMS_DEPRECIES:
        base["deprecatedApiNames"] = list(NOMS_DEPRECIES[api_name])
    return base


def metadata_payload(property_id: str) -> dict[str, object]:
    """Le corps de `GET /v1beta/properties/{id}/metadata` — dérivé du registre."""
    charge: dict[str, object] = {
        "name": f"properties/{property_id}/metadata",
        "dimensions": [
            _entree_metadata(
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
            _entree_metadata(
                m.api_name,
                {
                    "apiName": m.api_name,
                    "uiName": m.ui_name,
                    "description": m.description,
                    "category": m.category,
                    "type": m.type_metrique,
                },
            )
            for m in METRIQUES.values()
        ],
    }
    if property_id != "0":
        charge["comparisons"] = [dict(c) for c in COMPARAISONS_STANDARD]
    return charge

"""Les sessions du site vitrine de Boréal Conseil — le monde du mock.

Même univers que boondmanager-mock (l'ESN fictive de 34 personnes : agences
Paris/Lyon/Nantes, BU Data & IA / Cloud & Plateformes / Cybersécurité /
Transformation digitale) : le trafic web raconte la MÊME entreprise que le
CRM, pour que les jointures BI inter-sources tombent juste en dev.

Déterminisme, la règle qui gouverne tout le fichier :

  `build_day(seed, d)` ne dépend QUE de (seed, d) — `random.Random(f"{seed}:{d}")`,
  jamais `datetime.now()`, jamais l'état d'un autre jour. Étendre la timeline
  (horloge virtuelle avancée) n'a donc AUCUN moyen de réécrire l'historique,
  et chaque jour peut être matérialisé paresseusement puis mis en cache.

Le niveau de détail est la session INDIVIDUELLE, pas des rapports pré-agrégés :
c'est ce qui rend `totalUsers` exact (dédoublonnage réel d'identifiants
visiteurs sur n'importe quelle plage) au lieu d'une approximation additive qui
mentirait précisément là où GA4 est piégeux.

Identités visiteurs sans génération inter-jours : `planned_new(seed, B)` est
une fonction pure qui fixe combien d'identifiants NAISSENT le jour B. Un jour
D pioche ses visiteurs récurrents en tirant (B ≤ D, j < planned_new(B)) — d'où
des ids stables `AAAAMMJJ.1jjjjj` sans jamais matérialiser un autre jour. Un
tirage B == D est un même-jour (deuxième visite le jour de la première) : rare
et plausible, l'ordre intra-journée n'étant pas modélisé.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..clock import fuseau_paris
from ..settings import settings

DEBUT_HISTORIQUE = date(2025, 1, 1)

# ── Volumétrie ───────────────────────────────────────────────────────────────

_BASE_ORGANIQUE = 62.0
_CROISSANCE_ANNUELLE = 1.18

# facteur par jour de semaine (lundi=0) — un site B2B vit aux heures de bureau
_JOUR_SEMAINE = (1.05, 1.12, 1.10, 1.08, 0.92, 0.25, 0.18)

_FERIES: frozenset[date] = frozenset(
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


def _saison(d: date) -> float:
    """Fériés, ponts, été, fêtes : les creux d'un site B2B français."""
    if d in _FERIES:
        return 0.30
    # pont : vendredi après un jeudi férié, lundi avant un mardi férié
    if d.weekday() == 4 and (d - timedelta(days=1)) in _FERIES:
        return 0.60
    if d.weekday() == 0 and (d + timedelta(days=1)) in _FERIES:
        return 0.60
    if d.month == 8 and d.day <= 24:
        return 0.55
    if (d.month == 12 and d.day >= 22) or (d.month == 1 and d.day <= 2):
        return 0.50
    return 1.0


def _volume_organique(d: date) -> float:
    """Forme fermée, sans aléa : la même valeur quel que soit l'appelant."""
    croissance: float = _CROISSANCE_ANNUELLE ** ((d - DEBUT_HISTORIQUE).days / 365.0)
    return _BASE_ORGANIQUE * croissance * _JOUR_SEMAINE[d.weekday()] * _saison(d)


def planned_new(seed: int, d: date) -> int:
    """Nombre d'identifiants visiteurs qui NAISSENT le jour d.

    Fonction pure de (seed, d), avec son propre générateur : elle est appelée
    par d'autres jours (choix des visiteurs récurrents) et ne doit donc pas
    dépendre de l'ordre de consommation du rng de `build_day`. La part reste
    sous 0,65 * organique quand le volume total dépasse 0,85 * organique : un
    nouveau visiteur a TOUJOURS une session le jour de sa naissance.
    """
    rng = random.Random(f"{seed}:nouveaux:{d.isoformat()}")
    part = 0.50 + 0.15 * rng.random()
    return max(1, round(_volume_organique(d) * part))


# ── Campagnes ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Campagne:
    nom: str
    debut: date
    fin: date
    canal: str
    source: str
    medium: str
    extra_par_jour: int
    atterrissages: tuple[str, ...]
    mult_lead: float = 1.0
    mult_job: float = 1.0


# Le calendrier raconte la vie de l'ESN : recrutements (les annonces du monde
# boondmanager), webinaires, salon, livre blanc. Les multiplicateurs créent la
# corrélation attendue : le trafic de campagne convertit plus.
CAMPAGNES: tuple[Campagne, ...] = (
    Campagne(
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
    Campagne(
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
    Campagne(
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
    Campagne(
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
    Campagne(
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
    Campagne(
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
    Campagne(
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


def _rampe(c: Campagne, d: date) -> float:
    """Montée/descente sur 5 jours plutôt qu'un créneau rectangulaire."""
    if not (c.debut <= d <= c.fin):
        return 0.0
    return min(1.0, ((d - c.debut).days + 1) / 5.0, ((c.fin - d).days + 1) / 5.0)


# ── Arborescence du site (~32 pages, alignée sur le monde boondmanager) ─────

PAGE_ACCUEIL = "/"
PAGES_EXPERTISES = (
    "/expertises/data-ia",
    "/expertises/cloud-plateformes",
    "/expertises/cybersecurite",
    "/expertises/transformation-digitale",
)
PAGES_REALISATIONS = (
    "/realisations/refonte-plateforme-data-retail",
    "/realisations/migration-cloud-banque",
    "/realisations/socle-mlops-energie",
    "/realisations/tableau-de-bord-pilotage-finance",
    "/realisations/data-mesh-domaine-client",
    "/realisations/audit-securite-industrie",
)
PAGES_BLOG = (
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
PAGES_OFFRES = (
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
PAGE_A_PROPOS = "/a-propos"
_INDEX_EXPERTISES = "/expertises"
_INDEX_REALISATIONS = "/realisations"
_INDEX_BLOG = "/blog"
_INDEX_OFFRES = "/offres-emploi"

TOUTES_PAGES: tuple[str, ...] = (
    PAGE_ACCUEIL,
    _INDEX_EXPERTISES,
    *PAGES_EXPERTISES,
    _INDEX_REALISATIONS,
    *PAGES_REALISATIONS,
    _INDEX_BLOG,
    *PAGES_BLOG,
    _INDEX_OFFRES,
    *PAGES_OFFRES,
    PAGE_CONTACT,
    PAGE_A_PROPOS,
)


def _parcours() -> dict[str, tuple[str, ...]]:
    """Graphe de navigation : depuis chaque page, les suites plausibles.

    /contact est terminal — on ne navigue pas depuis un formulaire envoyé.
    """
    graphe: dict[str, tuple[str, ...]] = {
        PAGE_ACCUEIL: (
            _INDEX_EXPERTISES,
            _INDEX_REALISATIONS,
            _INDEX_BLOG,
            _INDEX_OFFRES,
            PAGE_A_PROPOS,
            PAGE_CONTACT,
        ),
        _INDEX_EXPERTISES: PAGES_EXPERTISES,
        _INDEX_REALISATIONS: PAGES_REALISATIONS,
        _INDEX_BLOG: PAGES_BLOG,
        _INDEX_OFFRES: PAGES_OFFRES,
        PAGE_CONTACT: (),
        PAGE_A_PROPOS: (PAGE_CONTACT, _INDEX_EXPERTISES, PAGE_ACCUEIL),
    }
    for page in PAGES_EXPERTISES:
        autres = tuple(p for p in PAGES_EXPERTISES if p != page)
        graphe[page] = (_INDEX_REALISATIONS, PAGE_CONTACT, *autres)
    for page in PAGES_REALISATIONS:
        autres = tuple(p for p in PAGES_REALISATIONS if p != page)
        graphe[page] = (*PAGES_EXPERTISES[:2], PAGE_CONTACT, *autres[:2])
    for page in PAGES_BLOG:
        autres = tuple(p for p in PAGES_BLOG if p != page)
        graphe[page] = (*autres[:3], PAGES_EXPERTISES[0], PAGE_CONTACT, _INDEX_BLOG)
    for page in PAGES_OFFRES:
        autres = tuple(p for p in PAGES_OFFRES if p != page)
        graphe[page] = (*autres[:2], PAGE_CONTACT, _INDEX_OFFRES)
    return graphe


_PARCOURS = _parcours()

# ── Mix d'acquisition organique, appareils, géographie ───────────────────────

# (canal, source, medium) — les canaux payants n'existent QUE via les
# campagnes : un ESN de 34 personnes n'a pas de cpc « always-on ».
_MIX_ORGANIQUE: tuple[tuple[tuple[str, str, str], int], ...] = (
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

# Repli de sessionCampaignName par canal organique — wording GA4 non attesté,
# consigné dans docs/UNVERIFIED-FIELDS.md. L'email est un vrai nom de campagne
# (newsletter mensuelle), calculé à part.
_CAMPAGNE_PAR_CANAL = {
    "Organic Search": "(organic)",
    "Organic Social": "(organic)",
    "Direct": "(direct)",
    "Referral": "(referral)",
    "Unassigned": "(not set)",
}

_MIX_APPAREILS: tuple[tuple[str, int], ...] = (("desktop", 62), ("mobile", 33), ("tablet", 5))

_OS_PAR_APPAREIL: dict[str, tuple[tuple[str, int], ...]] = {
    "desktop": (("Windows", 58), ("Macintosh", 30), ("Linux", 9), ("Chrome OS", 3)),
    "mobile": (("Android", 58), ("iOS", 42)),
    "tablet": (("iOS", 70), ("Android", 30)),
}

_NAVIGATEURS_PAR_OS: dict[str, tuple[tuple[str, int], ...]] = {
    "Windows": (("Chrome", 60), ("Edge", 28), ("Firefox", 12)),
    "Macintosh": (("Chrome", 45), ("Safari", 45), ("Firefox", 10)),
    "Linux": (("Firefox", 50), ("Chrome", 50)),
    "Chrome OS": (("Chrome", 100),),
    "Android": (("Chrome", 75), ("Samsung Internet", 25)),
    "iOS": (("Safari", 85), ("Chrome", 15)),
}

# (pays, région, ville) — noms géographiques anglais sans accents, comme les
# renvoie l'API GA (politique d'accents non attestée : UNVERIFIED-FIELDS.md).
_MIX_GEO: tuple[tuple[tuple[str, str, str], int], ...] = (
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

# atterrissage par canal : pondérations sur des familles de pages
_ATTERRISSAGE_PAR_CANAL: dict[str, tuple[tuple[tuple[str, ...], int], ...]] = {
    "Organic Search": (
        (PAGES_BLOG, 45),
        (PAGES_EXPERTISES, 25),
        ((PAGE_ACCUEIL,), 10),
        (PAGES_REALISATIONS, 12),
        (PAGES_OFFRES, 8),
    ),
    "Direct": (
        ((PAGE_ACCUEIL,), 55),
        ((_INDEX_OFFRES, *PAGES_OFFRES), 20),
        (PAGES_EXPERTISES, 12),
        ((PAGE_CONTACT,), 5),
        ((PAGE_A_PROPOS,), 8),
    ),
    "Referral": (
        (PAGES_REALISATIONS, 30),
        ((PAGE_ACCUEIL,), 30),
        ((_INDEX_OFFRES, *PAGES_OFFRES), 25),
        (PAGES_BLOG, 15),
    ),
    "Organic Social": (
        (PAGES_BLOG, 45),
        ((_INDEX_OFFRES, *PAGES_OFFRES), 30),
        ((PAGE_ACCUEIL,), 15),
        (PAGES_REALISATIONS, 10),
    ),
    "Email": (
        (PAGES_BLOG, 55),
        (PAGES_REALISATIONS, 20),
        ((PAGE_ACCUEIL,), 15),
        (PAGES_EXPERTISES, 10),
    ),
    "Unassigned": (((PAGE_ACCUEIL,), 100),),
}

# 0 à 5 pages de suite ; ~52 % de sessions mono-page pour un taux de rebond
# réaliste (30-40 %) une fois appliquée la règle d'engagement GA4.
_POIDS_NB_SUITES = (52, 22, 12, 8, 4, 2)

_HEURES_POIDS = (
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


# ── La session ───────────────────────────────────────────────────────────────


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


def _choix_pondere[T](rng: random.Random, mix: tuple[tuple[T, int], ...]) -> T:
    return rng.choices([v for v, _ in mix], weights=[p for _, p in mix], k=1)[0]


def _user_id(naissance: date, indice: int) -> str:
    """Forme « client id » GA : AAAAMMJJ.1jjjjj — stable entre les jours."""
    return f"{naissance:%Y%m%d}.1{indice:05d}"


def _naissance_visiteur(rng: random.Random, d: date, seed: int) -> tuple[date, int]:
    """Jour de naissance (biais de récence exponentiel) + indice du visiteur."""
    decalage = int(rng.expovariate(1 / 75.0)) + 1
    naissance = max(DEBUT_HISTORIQUE, d - timedelta(days=decalage))
    return naissance, rng.randrange(planned_new(seed, naissance))


def _marche(rng: random.Random, atterrissage: str) -> tuple[str, ...]:
    pages = [atterrissage]
    nb_suites = _choix_pondere(rng, tuple(zip(range(6), _POIDS_NB_SUITES, strict=True)))
    for _ in range(nb_suites):
        candidates = _PARCOURS.get(pages[-1], ())
        if not candidates:
            break
        pages.append(rng.choice(candidates))
    return tuple(pages)


def _horodatage(rng: random.Random, d: date) -> datetime:
    heure = _choix_pondere(rng, tuple(zip(range(24), _HEURES_POIDS, strict=True)))
    naif = datetime(d.year, d.month, d.day, heure, rng.randrange(60), rng.randrange(60))
    return naif.replace(tzinfo=fuseau_paris(naif))


def _acquisition(
    rng: random.Random, d: date, campagne: Campagne | None
) -> tuple[str, str, str, str, str]:
    """(canal, source, medium, nom de campagne, page d'atterrissage)."""
    if campagne is not None:
        return (
            campagne.canal,
            campagne.source,
            campagne.medium,
            campagne.nom,
            rng.choice(campagne.atterrissages),
        )
    canal, source, medium = _choix_pondere(rng, _MIX_ORGANIQUE)
    nom = f"newsletter-{d:%Y-%m}" if canal == "Email" else _CAMPAGNE_PAR_CANAL[canal]
    atterrissage = rng.choice(_choix_pondere(rng, _ATTERRISSAGE_PAR_CANAL[canal]))
    return canal, source, medium, nom, atterrissage


def _evenements(
    rng: random.Random,
    pages: tuple[str, ...],
    *,
    est_nouveau: bool,
    engage: bool,
    lead: bool,
    candidature: bool,
) -> tuple[tuple[str, int], ...]:
    evenements: list[tuple[str, int]] = [("session_start", 1), ("page_view", len(pages))]
    if est_nouveau:
        evenements.append(("first_visit", 1))
    if engage:
        evenements.append(("user_engagement", 1))
    defilements = sum(1 for _ in pages if rng.random() < 0.45)
    if defilements:
        evenements.append(("scroll", defilements))
    if lead:
        evenements.append(("generate_lead", 1))
    if candidature:
        evenements.append(("job_apply", 1))
    return tuple(evenements)


def _construire_session(
    rng: random.Random,
    seed: int,
    d: date,
    *,
    indice: int,
    nouveaux: int,
    campagne: Campagne | None,
) -> Session:
    if indice < nouveaux:
        est_nouveau, user_id = True, _user_id(d, indice)
    else:
        est_nouveau = False
        naissance, j = _naissance_visiteur(rng, d, seed)
        user_id = _user_id(naissance, j)

    canal, source, medium, nom_campagne, atterrissage = _acquisition(rng, d, campagne)
    appareil = _choix_pondere(rng, _MIX_APPAREILS)
    systeme = _choix_pondere(rng, _OS_PAR_APPAREIL[appareil])
    navigateur = _choix_pondere(rng, _NAVIGATEURS_PAR_OS[systeme])
    pays, region, ville = _choix_pondere(rng, _MIX_GEO)
    pages = _marche(rng, atterrissage)

    mult_lead = campagne.mult_lead if campagne else 1.0
    mult_job = campagne.mult_job if campagne else 1.0
    p_lead = 0.18 * mult_lead * (1.15 if appareil == "desktop" else 1.0)
    if not est_nouveau:
        p_lead *= 1.2
    lead = PAGE_CONTACT in pages and rng.random() < min(p_lead, 0.9)
    sur_offre = any(p.startswith("/offres-emploi/") for p in pages)
    candidature = sur_offre and rng.random() < min(0.06 * mult_job, 0.9)

    duree = (
        rng.randint(2, 25)
        if len(pages) == 1
        else 25 + 45 * (len(pages) - 1) + int(rng.expovariate(1 / 30.0))
    )
    engagement = min(int(duree * (0.6 + 0.3 * rng.random())), duree)
    engage = engagement >= 10 or lead or candidature or len(pages) >= 2

    evenements = _evenements(
        rng, pages, est_nouveau=est_nouveau, engage=engage, lead=lead, candidature=candidature
    )
    return Session(
        date=d,
        ts=_horodatage(rng, d),
        session_id=f"{d:%Y%m%d}.{indice}",
        user_id=user_id,
        is_new=est_nouveau,
        channel_group=canal,
        source=source,
        medium=medium,
        campaign=nom_campagne,
        device_category=appareil,
        browser=navigateur,
        operating_system=systeme,
        country=pays,
        region=region,
        city=ville,
        landing_page=atterrissage,
        pages=pages,
        events=evenements,
        duration_seconds=duree,
        engagement_seconds=engagement,
        engaged=engage,
        lag_hours=settings.freshness_hours * (rng.random() ** 1.6),
    )


def build_day(seed: int, d: date) -> tuple[Session, ...]:
    """Toutes les sessions du jour d — la fonction pure au cœur du mock.

    Ordre de construction : volume organique, extras de campagne, puis chaque
    session dans un ordre FIXE (les nouveaux visiteurs d'abord). Ne rien
    réordonner sans raison : tout changement d'ordre de consommation du rng
    change le monde, donc le contrat de fait des tests consommateurs.
    """
    rng = random.Random(f"{seed}:{d.isoformat()}")
    organique = round(_volume_organique(d) * (0.85 + 0.30 * rng.random()))
    volumes_campagnes: list[tuple[Campagne, int]] = []
    for c in CAMPAGNES:
        rampe = _rampe(c, d)
        if rampe > 0:
            extra = round(c.extra_par_jour * rampe * (0.7 + 0.6 * rng.random()))
            if extra:
                volumes_campagnes.append((c, extra))

    nouveaux = min(planned_new(seed, d), organique)
    sessions: list[Session] = []
    for i in range(organique):
        sessions.append(
            _construire_session(rng, seed, d, indice=i, nouveaux=nouveaux, campagne=None)
        )
    indice = organique
    for c, extra in volumes_campagnes:
        for _ in range(extra):
            sessions.append(
                _construire_session(rng, seed, d, indice=indice, nouveaux=nouveaux, campagne=c)
            )
            indice += 1
    return tuple(sessions)

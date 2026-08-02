"""Horloge virtuelle ANCRÉE.

boondmanager-mock base son horloge sur `time.time() + offset` ; ici la base
est FIXE, parce que `today`/`yesterday`/`NdaysAgo` font partie de la surface
d'API v1beta : un « today » wall-clock sortirait du monde généré (qui s'arrête
à l'ancre) et renverrait zéro ligne pour toujours. L'ancre reprend la date de
boondmanager-mock (15 juillet 2026) pour que les jointures BI inter-sources
tombent juste en dev — et rend chaque réponse déterministe à l'octet près.

Le décalage (`horloge.offset_secondes`) est piloté par /__admin/clock et remis
à zéro par `state.reset()` : avancer l'horloge fait vieillir ENSEMBLE les
dates relatives, l'expiration des bearers et la visibilité de fraîcheur.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

ANCRE = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone(timedelta(hours=2)))

_MOTIF_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_MOTIF_RELATIF = re.compile(r"(\d+)daysAgo")


class Horloge:
    def __init__(self) -> None:
        self.offset_secondes: float = 0.0


horloge = Horloge()


def fuseau_paris(utc: datetime) -> timezone:
    """Règle simplifiée reprise de boondmanager-mock : avril-octobre = +02:00,
    sinon +01:00. Fausse de quelques jours autour des bascules DST réelles —
    assumé : aucune métrique du mock ne dépend de l'heure exacte de bascule."""
    return timezone(timedelta(hours=2 if 4 <= utc.month <= 10 else 1))


def virtual_now() -> datetime:
    utc = (ANCRE + timedelta(seconds=horloge.offset_secondes)).astimezone(UTC)
    return utc.astimezone(fuseau_paris(utc))


def virtual_today() -> date:
    return virtual_now().date()


def resoudre_date(brut: str) -> date:
    """Grammaire des DateRange v1beta : `YYYY-MM-DD`, `today`, `yesterday`,
    `NdaysAgo` — sensible à la casse, comme le vrai service."""
    texte = brut.strip()
    if texte == "today":
        return virtual_today()
    if texte == "yesterday":
        return virtual_today() - timedelta(days=1)
    relatif = _MOTIF_RELATIF.fullmatch(texte)
    if relatif:
        return virtual_today() - timedelta(days=int(relatif.group(1)))
    if _MOTIF_ISO.fullmatch(texte):
        return date.fromisoformat(texte)
    raise ValueError(brut)

"""Le pipeline runReport — cœur du dialecte v1beta.

Étapes, dans l'ordre du vrai service : validation → résolution des dates
(horloge VIRTUELLE) → matérialisation des jours visibles → fan-out éventuel
(pagePath/eventName) → filtre de dimensions → groupement → calcul des
métriques → filtre de métriques (having) → tri → agrégations → pagination →
sérialisation proto3-JSON (champs répétés vides OMIS, valeurs de métriques en
CHAÎNES, int64 en chaînes, int32 en nombres).

Chaque étape est une petite fonction : le pipeline se lit de haut en bas dans
`executer_run_report`, et aucune étape ne peut être contournée par une route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi.responses import JSONResponse

from .clock import resoudre_date
from .errors import erreur
from .registry import (
    DIMENSIONS,
    METRIQUES,
    TYPE_INTEGER,
    Accumulateur,
    Dimension,
    Metrique,
    Unite,
)
from .settings import CURRENCY_CODE, TIME_ZONE
from .state import state

LIMITE_DEFAUT = 10_000
LIMITE_MAX = 250_000
MAX_DIMENSIONS = 9
MAX_METRIQUES = 10
MAX_PLAGES = 4
AGREGATIONS_SUPPORTEES = ("TOTAL", "MAXIMUM", "MINIMUM")
# Aiguillages proto refusés explicitement plutôt qu'ignorés en silence : un
# consommateur qui les enverrait croirait, sinon, qu'ils ont agi.
CHAMPS_NON_SUPPORTES = ("cohortSpec", "comparisons")

DIMENSION_PLAGE = "dateRange"


class ErreurRequete(Exception):
    """Requête invalide → 400 INVALID_ARGUMENT, message façon Google."""


@dataclass(frozen=True, slots=True)
class Plage:
    nom: str
    debut: date
    fin: date


@dataclass(slots=True)
class Requete:
    """La requête runReport une fois validée — plus aucun `dict` au-delà."""

    dimensions: list[Dimension]
    metriques: list[Metrique]
    plages: list[Plage]
    limite: int
    decalage: int
    tris: list[dict[str, Any]] = field(default_factory=list)
    filtre_dimensions: dict[str, Any] | None = None
    filtre_metriques: dict[str, Any] | None = None
    agregations: list[str] = field(default_factory=list)
    lignes_vides: bool = False
    quota: bool = False

    @property
    def multi_plages(self) -> bool:
        return len(self.plages) > 1

    @property
    def fan_page(self) -> bool:
        return any(d.portee == "page" for d in self.dimensions)

    @property
    def fan_event(self) -> bool:
        return any(d.portee == "event" for d in self.dimensions)


@dataclass(slots=True)
class Ligne:
    dims: tuple[str, ...]  # valeurs des dimensions régulières
    plage: str  # nom de la plage (label dateRange)
    valeurs: list[float | int] = field(default_factory=list)


# ── Validation ───────────────────────────────────────────────────────────────


def _noms(corps: dict[str, Any], champ: str, registre: dict[str, Any], sorte: str) -> list[str]:
    brut = corps.get(champ) or []
    if not isinstance(brut, list):
        raise ErreurRequete(f"Invalid value for {champ}.")
    noms: list[str] = []
    for entree in brut:
        nom = entree.get("name") if isinstance(entree, dict) else None
        if not isinstance(nom, str) or not nom:
            raise ErreurRequete(f"Invalid value for {champ}.")
        if nom not in registre:
            raise ErreurRequete(f"Field {nom} is not a valid {sorte}.")
        if nom in noms:
            raise ErreurRequete(f"Field {nom} is duplicated in the request.")
        noms.append(nom)
    return noms


def _entier64(corps: dict[str, Any], champ: str, defaut: int) -> int:
    """int64 proto3 : accepté en nombre JSON OU en chaîne — les deux formes
    sont légales sur le fil, un client ne doit pas être puni pour l'une."""
    brut = corps.get(champ)
    if brut is None:
        return defaut
    if isinstance(brut, bool):
        raise ErreurRequete(f"Invalid value for {champ}.")
    if isinstance(brut, int):
        return brut
    if isinstance(brut, str):
        texte = brut.strip()
        signe = texte[1:] if texte.startswith("-") else texte
        if signe.isdigit():
            return int(texte)
    raise ErreurRequete(f"Invalid value for {champ}.")


def _plages(corps: dict[str, Any]) -> list[Plage]:
    brut = corps.get("dateRanges")
    if not isinstance(brut, list) or not brut:
        raise ErreurRequete("Requests must specify at least one date range.")
    if len(brut) > MAX_PLAGES:
        raise ErreurRequete(f"Requests are limited to {MAX_PLAGES} date ranges.")
    plages: list[Plage] = []
    for i, entree in enumerate(brut):
        if not isinstance(entree, dict):
            raise ErreurRequete("Invalid value for dateRanges.")
        try:
            debut = resoudre_date(str(entree.get("startDate", "")))
            fin = resoudre_date(str(entree.get("endDate", "")))
        except ValueError as exc:
            raise ErreurRequete(f"Invalid date: {exc.args[0]}.") from exc
        if debut > fin:
            raise ErreurRequete("The start date cannot be after the end date.")
        nom = entree.get("name") or f"date_range_{i}"
        plages.append(Plage(str(nom), debut, fin))
    return plages


def _agregations(corps: dict[str, Any]) -> list[str]:
    brut = corps.get("metricAggregations") or []
    if not isinstance(brut, list):
        raise ErreurRequete("Invalid value for metricAggregations.")
    for a in brut:
        if a not in AGREGATIONS_SUPPORTEES:
            raise ErreurRequete(f"Metric aggregation {a} is not supported by this mock.")
    return [str(a) for a in brut]


def _tris(corps: dict[str, Any], dims: list[str], mets: list[str]) -> list[dict[str, Any]]:
    brut = corps.get("orderBys") or []
    if not isinstance(brut, list):
        raise ErreurRequete("Invalid value for orderBys.")
    for tri in brut:
        if not isinstance(tri, dict):
            raise ErreurRequete("Invalid value for orderBys.")
        metrique = tri.get("metric")
        dimension = tri.get("dimension")
        if (metrique is None) == (dimension is None):
            raise ErreurRequete("Each order by must target exactly one metric or dimension.")
        if metrique is not None and metrique.get("metricName") not in mets:
            raise ErreurRequete("Order bys can only target metrics requested in the report.")
        if dimension is not None and dimension.get("dimensionName") not in [
            *dims,
            DIMENSION_PLAGE,
        ]:
            raise ErreurRequete("Order bys can only target dimensions requested in the report.")
    return [dict(tri) for tri in brut]


def valider(corps: dict[str, Any]) -> Requete:
    for champ in CHAMPS_NON_SUPPORTES:
        if champ in corps:
            raise ErreurRequete(f"{champ} is not supported by this mock.")
    noms_metriques = _noms(corps, "metrics", METRIQUES, "metric")
    if not noms_metriques:
        raise ErreurRequete("Requests must specify at least one metric.")
    if len(noms_metriques) > MAX_METRIQUES:
        raise ErreurRequete(f"Requests are limited to {MAX_METRIQUES} metrics.")
    noms_dimensions = _noms(corps, "dimensions", DIMENSIONS, "dimension")
    if len(noms_dimensions) > MAX_DIMENSIONS:
        raise ErreurRequete(f"Requests are limited to {MAX_DIMENSIONS} dimensions.")
    limite = _entier64(corps, "limit", LIMITE_DEFAUT)
    if limite < 0:
        raise ErreurRequete("Invalid value for limit.")
    if limite == 0:
        limite = LIMITE_DEFAUT
    decalage = _entier64(corps, "offset", 0)
    if decalage < 0:
        raise ErreurRequete("Invalid value for offset.")
    return Requete(
        dimensions=[DIMENSIONS[n] for n in noms_dimensions],
        metriques=[METRIQUES[n] for n in noms_metriques],
        plages=_plages(corps),
        # Plafonné EN SILENCE, pas rejeté : c'est le comportement documenté.
        limite=min(limite, LIMITE_MAX),
        decalage=decalage,
        tris=_tris(corps, noms_dimensions, noms_metriques),
        filtre_dimensions=corps.get("dimensionFilter"),
        filtre_metriques=corps.get("metricFilter"),
        agregations=_agregations(corps),
        lignes_vides=bool(corps.get("keepEmptyRows", False)),
        quota=bool(corps.get("returnPropertyQuota", False)),
    )


# ── Collecte et groupement ───────────────────────────────────────────────────


def _jours(plage: Plage) -> list[date]:
    return [plage.debut + timedelta(days=i) for i in range((plage.fin - plage.debut).days + 1)]


def _unites(requete: Requete, d: date) -> list[Unite]:
    sessions = state.visible_sessions(d)
    if requete.fan_page and requete.fan_event:
        # Produit croisé assumé : GA attribue l'événement à SA page ; le mock
        # ne modélise pas ce lien — approximation consignée (UNVERIFIED).
        return [
            Unite(s, page=p, evenement=nom, n_evenements=n)
            for s in sessions
            for p in s.pages
            for nom, n in s.events
        ]
    if requete.fan_page:
        return [Unite(s, page=p) for s in sessions for p in s.pages]
    if requete.fan_event:
        return [Unite(s, evenement=nom, n_evenements=n) for s in sessions for nom, n in s.events]
    return [Unite(s) for s in sessions]


def collecter(
    requete: Requete,
) -> tuple[dict[tuple[str, ...], Accumulateur], dict[str, Accumulateur]]:
    """Groupes par clé (valeurs de dims + nom de plage) + global par plage."""
    groupes: dict[tuple[str, ...], Accumulateur] = {}
    globaux: dict[str, Accumulateur] = {}
    fan_page, fan_event = requete.fan_page, requete.fan_event
    for plage in requete.plages:
        global_plage = globaux.setdefault(plage.nom, Accumulateur())
        for jour in _jours(plage):
            for unite in _unites(requete, jour):
                cle = (
                    *(dim.extraire(unite) for dim in requete.dimensions),
                    plage.nom,
                )
                acc = groupes.get(cle)
                if acc is None:
                    acc = groupes[cle] = Accumulateur()
                acc.ajouter(unite, fan_page=fan_page, fan_event=fan_event)
                global_plage.ajouter(unite, fan_page=fan_page, fan_event=fan_event)
    return groupes, globaux


def _spine(requete: Requete, groupes: dict[tuple[str, ...], Accumulateur]) -> None:
    """keepEmptyRows : complète le calendrier quand TOUTES les dimensions sont
    de la famille date — le cas BI « série temporelle sans trous ». Les autres
    familles ne sont pas synthétisées (limitation documentée)."""
    if not requete.dimensions or not all(d.depuis_date for d in requete.dimensions):
        return
    for plage in requete.plages:
        for jour in _jours(plage):
            cle = (
                *(d.depuis_date(jour) if d.depuis_date else "" for d in requete.dimensions),
                plage.nom,
            )
            groupes.setdefault(cle, Accumulateur())


def calculer_lignes(requete: Requete, groupes: dict[tuple[str, ...], Accumulateur]) -> list[Ligne]:
    lignes = []
    for cle, acc in groupes.items():
        valeurs: list[float | int] = [m.calculer(acc) for m in requete.metriques]
        lignes.append(Ligne(dims=cle[:-1], plage=cle[-1], valeurs=valeurs))
    return lignes


# ── Tri ──────────────────────────────────────────────────────────────────────


def _cle_dimension(valeur: str, type_tri: str) -> Any:
    if type_tri == "NUMERIC":
        try:
            return float(valeur)
        except ValueError:
            return 0.0
    if type_tri == "CASE_INSENSITIVE_ALPHANUMERIC":
        return valeur.casefold()
    return valeur


def ordonner(requete: Requete, lignes: list[Ligne]) -> None:
    """`orderBys` s'ils sont fournis, sinon un ordre par défaut DÉTERMINISTE
    (première métrique décroissante puis dimensions croissantes). L'ordre par
    défaut du vrai service n'est pas attesté — UNVERIFIED — mais un mock ne
    doit jamais rendre un ordre qui dépend du hasard d'un dict."""
    if not requete.tris:
        lignes.sort(key=lambda ligne: (-float(ligne.valeurs[0]), ligne.dims, ligne.plage))
        return
    noms_dims = [d.api_name for d in requete.dimensions]
    noms_mets = [m.api_name for m in requete.metriques]
    # tris stables appliqués du dernier au premier : le premier orderBy domine
    for tri in reversed(requete.tris):
        desc = bool(tri.get("desc", False))
        if tri.get("metric") is not None:
            indice = noms_mets.index(tri["metric"]["metricName"])

            def par_metrique(ligne: Ligne, i: int = indice) -> float:
                return float(ligne.valeurs[i])

            lignes.sort(key=par_metrique, reverse=desc)
        else:
            nom = tri["dimension"]["dimensionName"]
            type_tri = tri["dimension"].get("orderType", "ALPHANUMERIC")
            if nom == DIMENSION_PLAGE:
                lignes.sort(key=lambda ligne: ligne.plage, reverse=desc)
            else:
                indice = noms_dims.index(nom)

                def par_dimension(ligne: Ligne, i: int = indice, t: str = type_tri) -> Any:
                    return _cle_dimension(ligne.dims[i], t)

                lignes.sort(key=par_dimension, reverse=desc)


# ── Sérialisation ────────────────────────────────────────────────────────────


def _valeur_metrique(valeur: float | int, type_metrique: str) -> str:
    """Toujours une CHAÎNE — la règle proto3-JSON qui surprend tout le monde."""
    if type_metrique == TYPE_INTEGER:
        return str(int(valeur))
    nombre = float(valeur)
    return str(int(nombre)) if nombre.is_integer() else repr(nombre)


def _ligne_json(requete: Requete, ligne: Ligne) -> dict[str, Any]:
    dims = [{"value": v} for v in ligne.dims]
    if requete.multi_plages:
        dims.append({"value": ligne.plage})
    corps: dict[str, Any] = {}
    if dims:
        corps["dimensionValues"] = dims
    corps["metricValues"] = [
        {"value": _valeur_metrique(v, m.type_metrique)}
        for v, m in zip(ligne.valeurs, requete.metriques, strict=True)
    ]
    return corps


def _lignes_agregations(
    requete: Requete, globaux: dict[str, Accumulateur], lignes: list[Ligne]
) -> dict[str, list[dict[str, Any]]]:
    """totals/maximums/minimums — une ligne par plage.

    TOTAL vient de l'accumulateur GLOBAL de la plage (dédoublonnage exact des
    utilisateurs, là où une somme de lignes sur-compterait), calculé AVANT le
    filtre de métriques — interaction réelle non attestée, UNVERIFIED.
    MAXIMUM/MINIMUM viennent des lignes finales.
    """
    resultat: dict[str, list[dict[str, Any]]] = {}
    if not requete.agregations or not lignes:
        return resultat
    marqueurs = {"TOTAL": "RESERVED_TOTAL", "MAXIMUM": "RESERVED_MAX", "MINIMUM": "RESERVED_MIN"}
    cles = {"TOTAL": "totals", "MAXIMUM": "maximums", "MINIMUM": "minimums"}
    for agregation in requete.agregations:
        lignes_agg: list[dict[str, Any]] = []
        for plage in requete.plages:
            if agregation == "TOTAL":
                acc = globaux[plage.nom]
                valeurs = [m.calculer(acc) for m in requete.metriques]
            else:
                candidates = [ligne.valeurs for ligne in lignes if ligne.plage == plage.nom] or [
                    [0] * len(requete.metriques)
                ]
                selection = max if agregation == "MAXIMUM" else min
                valeurs = [
                    selection(float(v[i]) for v in candidates)
                    for i in range(len(requete.metriques))
                ]
            dims = [{"value": marqueurs[agregation]} for _ in requete.dimensions]
            if requete.multi_plages:
                dims.append({"value": plage.nom})
            ligne_json: dict[str, Any] = {}
            if dims:
                ligne_json["dimensionValues"] = dims
            ligne_json["metricValues"] = [
                {"value": _valeur_metrique(v, m.type_metrique)}
                for v, m in zip(valeurs, requete.metriques, strict=True)
            ]
            lignes_agg.append(ligne_json)
        resultat[cles[agregation]] = lignes_agg
    return resultat


def serialiser(
    requete: Requete,
    lignes: list[Ligne],
    globaux: dict[str, Accumulateur],
) -> dict[str, Any]:
    """Assemble la réponse en OMETTANT les champs répétés vides et les int32 à
    zéro — proto3-JSON. Pas de `"rows": []`, pas de `"rowCount": 0`."""
    total = len(lignes)
    page = lignes[requete.decalage : requete.decalage + requete.limite]
    corps: dict[str, Any] = {}
    noms_dims = [{"name": d.api_name} for d in requete.dimensions]
    if requete.multi_plages:
        noms_dims.append({"name": DIMENSION_PLAGE})
    if noms_dims:
        corps["dimensionHeaders"] = noms_dims
    corps["metricHeaders"] = [
        {"name": m.api_name, "type": m.type_metrique} for m in requete.metriques
    ]
    if page:
        corps["rows"] = [_ligne_json(requete, ligne) for ligne in page]
    corps.update(_lignes_agregations(requete, globaux, lignes))
    if total:
        corps["rowCount"] = total
    corps["metadata"] = {"currencyCode": CURRENCY_CODE, "timeZone": TIME_ZONE}
    corps["kind"] = "analyticsData#runReport"
    return corps


# ── Le pipeline assemblé ─────────────────────────────────────────────────────


def executer_run_report(corps: dict[str, Any]) -> JSONResponse:
    try:
        requete = valider(corps)
    except ErreurRequete as exc:
        return erreur(400, str(exc))
    groupes, globaux = collecter(requete)
    if requete.lignes_vides:
        _spine(requete, groupes)
    lignes = calculer_lignes(requete, groupes)
    ordonner(requete, lignes)
    return JSONResponse(serialiser(requete, lignes, globaux))

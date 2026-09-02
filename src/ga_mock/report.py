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

from .clock import resoudre_date
from .errors import detail_bad_request
from .filters import ErreurFiltre, Predicat, compiler
from .registry import (
    DIMENSIONS,
    METRIQUES,
    TYPE_INTEGER,
    Accumulateur,
    Dimension,
    Metrique,
    Unite,
)
from .settings import CURRENCY_CODE, TIME_ZONE, settings
from .state import state

LIMITE_DEFAUT = 10_000
LIMITE_MAX = 250_000
MAX_DIMENSIONS = 9
MAX_METRIQUES = 10
MAX_PLAGES = 4
AGREGATIONS_SUPPORTEES = ("TOTAL", "MAXIMUM", "MINIMUM")
# `COUNT` est dans l'énumération du proto mais le service le REFUSE (relevé) ;
# `METRIC_AGGREGATION_UNSPECIFIED` est la valeur zéro, sans effet.
AGREGATION_INEXPLOITEE = "METRIC_AGGREGATION_UNSPECIFIED"
AGREGATIONS_CONNUES = (*AGREGATIONS_SUPPORTEES, "COUNT", AGREGATION_INEXPLOITEE)
# Aiguillages proto refusés explicitement plutôt qu'ignorés en silence : un
# consommateur qui les enverrait croirait, sinon, qu'ils ont agi.
CHAMPS_NON_SUPPORTES = ("cohortSpec", "comparisons")

# Le champ set RÉEL de RunReportRequest. Tout le reste est refusé par la couche
# de transcodage JSON du vendeur AVANT que la méthode ne voie la requête — un
# `dateRange` au lieu de `dateRanges` doit casser ICI.
CHAMPS_CONNUS = frozenset(
    {
        "property",
        "dimensions",
        "metrics",
        "dateRanges",
        "dimensionFilter",
        "metricFilter",
        "offset",
        "limit",
        "metricAggregations",
        "orderBys",
        "currencyCode",
        "cohortSpec",
        "keepEmptyRows",
        "returnPropertyQuota",
        "comparisons",
    }
)
# Les chemins des violations sont en snake_case : ce sont des noms de champs
# PROTO, pas les clés JSON de la requête.
CHAMPS_IMBRIQUES = {
    "dateRanges": ("date_ranges", {"startDate", "endDate", "name"}),
    "dimensions": ("dimensions", {"name", "dimensionExpression"}),
    "metrics": ("metrics", {"name", "expression", "invisible"}),
}

# Bornes de dates du service — des CONSTANTES, pas la date de création de la
# propriété : `2015-08-13` est refusée, `2015-08-14` passe.
DATE_MIN = date(2015, 8, 13)
DATE_MAX = date(3000, 1, 1)

URL_SCHEMA = "https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema"

DIMENSION_PLAGE = "dateRange"

# Coût en jetons de quota. Le vrai coût N'EST PAS forfaitaire : il croît avec
# l'etendue de la plage et le nombre de cellules (dimensions x metriques).
# Modèle calé sur sept mesures relevées sur une vraie propriété — cf.
# docs/CONFORMITE-REELLE.md ; c'est une INTERPOLATION, pas la formule de
# Google (consigné : quota-cout-forfaitaire).
COUT_JETONS_BASE = 1
COUT_JOURS_PAR_JETON = 60
COUT_CELLULES_FRANCHISE = 3
COUT_CELLULES_PAR_JETON = 32


class ErreurRequete(Exception):
    """Requête invalide → 400 INVALID_ARGUMENT, message du vendeur.

    `details` porte le `google.rpc.BadRequest` que la couche de transcodage
    JSON joint à SES erreurs (champ inconnu, valeur d'énumération invalide) —
    les erreurs de la méthode elle-même n'en ont pas.
    """

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details


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
    dimensions_filtre: list[Dimension] = field(default_factory=list)
    metriques_filtre: list[Metrique] = field(default_factory=list)
    tris: list[dict[str, Any]] = field(default_factory=list)
    pred_dimensions: Predicat | None = None
    pred_metriques: Predicat | None = None
    agregations: list[str] = field(default_factory=list)
    lignes_vides: bool = False
    quota: bool = False
    devise: str = CURRENCY_CODE

    @property
    def multi_plages(self) -> bool:
        return len(self.plages) > 1

    @property
    def toutes_dimensions(self) -> list[Dimension]:
        """Les demandées PUIS celles que seul le filtre cite : le fan-out et
        l'extraction doivent couvrir les deux, le groupement seulement les
        premières."""
        return [*self.dimensions, *self.dimensions_filtre]

    @property
    def fan_page(self) -> bool:
        return any(d.portee == "page" for d in self.toutes_dimensions)

    @property
    def fan_event(self) -> bool:
        return any(d.portee == "event" for d in self.toutes_dimensions)


@dataclass(slots=True)
class Ligne:
    dims: tuple[str, ...]  # valeurs des dimensions régulières
    plage: str  # nom de la plage (label dateRange)
    valeurs: list[float | int] = field(default_factory=list)


# ── Validation ───────────────────────────────────────────────────────────────


def champ_invalide(nom: str, sorte: str) -> ErreurRequete:
    """Le message du vendeur, à l'espacement près.

    Oui : UNE espace après « dimension. », DEUX après « metric. ». C'est le
    service qui écrit ça, et un test de consommateur qui compare la chaîne
    complète doit passer contre le mock. La suggestion « Did you mean … ? » que
    le vrai service préfixe n'est PAS reproduite (consigné).
    """
    separateur = " " if sorte == "dimension" else "  "
    return ErreurRequete(
        f"Field {nom} is not a valid {sorte}.{separateur}"
        f"For a list of valid dimensions and metrics, see {URL_SCHEMA} "
    )


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
            raise champ_invalide(nom, sorte)
        if nom in noms:
            raise ErreurRequete(
                f"Found duplicate dimensions: {nom}"
                if sorte == "dimension"
                else f"Duplicate metrics are not allowed. Found duplicate metrics: {nom}"
            )
        noms.append(nom)
    return noms


def _champs_inconnus(corps: dict[str, Any]) -> None:
    """Refus des clés JSON inconnues, AVANT tout le reste.

    Chez le vendeur c'est la couche de transcodage qui parle, pas la méthode :
    la requête n'atteint jamais l'API. Toutes les clés fautives sont listées,
    une violation chacune, et le message est leur concaténation.
    """
    violations: list[tuple[str, str]] = [
        ("", f'Invalid JSON payload received. Unknown name "{cle}": Cannot find field.')
        for cle in corps
        if cle not in CHAMPS_CONNUS
    ]
    for cle, (chemin_proto, admis) in CHAMPS_IMBRIQUES.items():
        entrees = corps.get(cle)
        if not isinstance(entrees, list):
            continue
        for i, entree in enumerate(entrees):
            if not isinstance(entree, dict):
                continue
            violations += [
                (
                    "",
                    f'Invalid JSON payload received. Unknown name "{sous}" '
                    f"at '{chemin_proto}[{i}]': Cannot find field.",
                )
                for sous in entree
                if sous not in admis
            ]
    if violations:
        raise ErreurRequete(
            "\n".join(description for _, description in violations),
            detail_bad_request(violations),
        )


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


def _borne_date(nom_proto: str, valeur: date) -> None:
    if not (DATE_MIN < valeur < DATE_MAX):
        raise ErreurRequete(
            f"{nom_proto} = {valeur.isoformat()} must be greater than "
            f"{DATE_MIN.isoformat()} and less than {DATE_MAX.isoformat()}."
        )


def _plages(corps: dict[str, Any]) -> list[Plage]:
    brut = corps.get("dateRanges")
    if not isinstance(brut, list) or not brut:
        raise ErreurRequete("A dateRange is required.")
    if len(brut) > MAX_PLAGES:
        raise ErreurRequete(
            f"Requests are limited to {MAX_PLAGES} dateRanges.\n"
            f"  This request contains {len(brut)} dateRanges."
        )
    plages: list[Plage] = []
    for i, entree in enumerate(brut):
        if not isinstance(entree, dict):
            raise ErreurRequete("Invalid value for dateRanges.")
        bornes: list[date] = []
        for cle in ("startDate", "endDate"):
            texte = str(entree.get(cle, ""))
            try:
                bornes.append(resoudre_date(texte))
            except ValueError as exc:
                raise ErreurRequete(
                    f"Invalid {cle} : {texte}. {cle} must be YYYY-MM-DD, "
                    "NdaysAgo, yesterday, or today."
                ) from exc
        debut, fin = bornes
        # L'ordre des contrôles est celui du service : les bornes fixes AVANT
        # la cohérence début/fin (une plage inversée hors bornes sort sur la
        # borne, pas sur l'inversion).
        _borne_date("start_date", debut)
        _borne_date("end_date", fin)
        if debut > fin:
            raise ErreurRequete(
                "start_date must be less than or equal to end_date. "
                f"start_date = {debut.isoformat()} and end_date = {fin.isoformat()}"
            )
        nom = entree.get("name") or f"date_range_{i}"
        plages.append(Plage(str(nom), debut, fin))
    return plages


def _agregations(corps: dict[str, Any]) -> list[str]:
    """`COUNT` fait partie de l'énumération proto mais le service le REFUSE ;
    une valeur hors énumération échoue plus tôt, dans le transcodage JSON — les
    deux messages sont donc différents, et les deux sont relevés."""
    brut = corps.get("metricAggregations") or []
    if not isinstance(brut, list):
        raise ErreurRequete("Invalid value for metricAggregations.")
    for i, a in enumerate(brut):
        if a not in AGREGATIONS_CONNUES:
            champ = f"metric_aggregations[{i}]"
            violation = (
                f"Invalid value at '{champ}' "
                "(type.googleapis.com/google.analytics.data.v1beta.MetricAggregation), "
                f'"{a}"'
            )
            raise ErreurRequete(violation, detail_bad_request([(champ, violation)]))
        if a == "COUNT":
            raise ErreurRequete("Metric aggregation Count is not supported in ReportRequest.")
    return [str(a) for a in brut if a != AGREGATION_INEXPLOITEE]


def _tris(corps: dict[str, Any], dims: list[str], mets: list[str]) -> list[dict[str, Any]]:
    """Un `orderBy` ne peut viser QUE des champs demandés — contrainte réelle,
    contrairement aux filtres. Le message nomme le champ fautif (chaîne vide
    quand l'entrée ne cible ni métrique ni dimension)."""
    brut = corps.get("orderBys") or []
    if not isinstance(brut, list):
        raise ErreurRequete("Invalid value for orderBys.")
    admis = {*dims, *mets, DIMENSION_PLAGE}
    for tri in brut:
        if not isinstance(tri, dict):
            raise ErreurRequete("Invalid value for orderBys.")
        metrique, dimension = tri.get("metric"), tri.get("dimension")
        vise = ""
        if isinstance(metrique, dict):
            vise = str(metrique.get("metricName", ""))
        elif isinstance(dimension, dict):
            vise = str(dimension.get("dimensionName", ""))
        if vise not in admis:
            raise ErreurRequete(
                f"Field {vise} exists in OrderBy but is not defined in input "
                "Dimensions/Metrics list"
            )
    return [dict(tri) for tri in brut]


def _champs_filtre(brut: Any) -> list[str]:
    """Les `fieldName` cités par un arbre de filtre, dans l'ordre de lecture."""
    champs: list[str] = []
    if isinstance(brut, dict):
        for cle, valeur in brut.items():
            if cle == "fieldName" and isinstance(valeur, str):
                champs.append(valeur)
            else:
                champs += _champs_filtre(valeur)
    elif isinstance(brut, list):
        for valeur in brut:
            champs += _champs_filtre(valeur)
    return champs


def _compiler_filtre(brut: Any, sorte: str) -> tuple[Predicat | None, list[str]]:
    """Compile un filtre et retourne AUSSI les champs qu'il cite.

    Relevé sur le service : un filtre n'a PAS à porter sur un champ demandé
    dans le rapport — filtrer sur `country` en groupant par `date` marche. Le
    mock devait donc cesser de l'exiger, et sait maintenant extraire les
    dimensions citées par le seul filtre.
    """
    if brut is None:
        return None, []
    if not isinstance(brut, dict):
        raise ErreurRequete(f"Invalid value for {sorte}Filter.")
    registre = DIMENSIONS if sorte == "dimension" else METRIQUES
    autre = METRIQUES if sorte == "dimension" else DIMENSIONS
    champs = _champs_filtre(brut)
    for nom in champs:
        if nom in registre:
            continue
        # Citer une métrique dans un dimensionFilter (ou l'inverse) a son
        # propre message chez le vendeur — deux diagnostics distincts.
        if nom in autre and sorte == "dimension":
            raise ErreurRequete("Found duplicate dimensions/metrics.")
        raise champ_invalide(nom, sorte)
    try:
        return compiler(brut, champs, sorte), champs
    except ErreurFiltre as exc:
        raise ErreurRequete(str(exc)) from exc


def valider(corps: dict[str, Any]) -> Requete:
    """L'ORDRE des contrôles est celui du service, relevé cas par cas.

    Il n'a rien d'évident : la validité des NOMS de champs et la positivité de
    `limit` passent AVANT l'exigence d'une plage de dates, alors que les bornes
    de cardinalité (9 dimensions, 10 métriques) passent APRÈS. Un consommateur
    qui envoie une requête doublement fautive doit recevoir ici le message
    qu'il recevra en prod, pas un autre.
    """
    # 1. Transcodage JSON : champs inconnus, valeurs d'énumération.
    _champs_inconnus(corps)
    for champ in CHAMPS_NON_SUPPORTES:
        if champ in corps:
            raise ErreurRequete(f"{champ} is not supported by this mock.")
    # 2. Validité des noms et doublons.
    noms_metriques = _noms(corps, "metrics", METRIQUES, "metric")
    noms_dimensions = _noms(corps, "dimensions", DIMENSIONS, "dimension")
    # 3. Pagination.
    limite = _entier64(corps, "limit", LIMITE_DEFAUT)
    if limite < 0:
        raise ErreurRequete(f"limit must be positive. The API received limit = {limite}")
    if limite == 0:
        limite = LIMITE_DEFAUT
    decalage = _entier64(corps, "offset", 0)
    if decalage < 0:
        raise ErreurRequete(f"offset must be positive. The API received offset = {decalage}")
    # 4. Plages de dates — AVANT les bornes de cardinalité : dix dimensions
    #    sans plage sortent sur « A dateRange is required. ».
    plages = _plages(corps)
    # 5. Bornes de cardinalité.
    if len(noms_metriques) > MAX_METRIQUES:
        raise ErreurRequete(
            f"Requests are limited to {MAX_METRIQUES} metrics within a nested request.\n"
            f"  This request is for {len(noms_metriques)} metrics."
        )
    if len(noms_dimensions) > MAX_DIMENSIONS:
        raise ErreurRequete(
            f"Requests are limited to {MAX_DIMENSIONS} dimensions within a nested request.\n"
            f"  This request is for {len(noms_dimensions)} dimensions."
        )
    # 6. Un rapport SANS métrique est valide : il rend des lignes de dimensions
    #    nues, sans `metricHeaders` ni `metricValues`. Seule l'absence des DEUX
    #    est refusée.
    if not noms_metriques and not noms_dimensions:
        raise ErreurRequete(
            "Requests require dimensions and/or metrics. Most requests include both."
        )
    pred_dimensions, champs_dimension = _compiler_filtre(corps.get("dimensionFilter"), "dimension")
    pred_metriques, champs_metrique = _compiler_filtre(corps.get("metricFilter"), "metric")
    return Requete(
        dimensions=[DIMENSIONS[n] for n in noms_dimensions],
        metriques=[METRIQUES[n] for n in noms_metriques],
        # Dimensions citées par le SEUL filtre : extraites pour l'évaluer, mais
        # jamais groupées ni rendues.
        dimensions_filtre=[
            DIMENSIONS[n] for n in dict.fromkeys(champs_dimension) if n not in noms_dimensions
        ],
        metriques_filtre=[
            METRIQUES[n] for n in dict.fromkeys(champs_metrique) if n not in noms_metriques
        ],
        plages=plages,
        # Plafonné EN SILENCE, pas rejeté : c'est le comportement documenté.
        limite=min(limite, LIMITE_MAX),
        decalage=decalage,
        tris=_tris(corps, noms_dimensions, noms_metriques),
        pred_dimensions=pred_dimensions,
        pred_metriques=pred_metriques,
        agregations=_agregations(corps),
        lignes_vides=bool(corps.get("keepEmptyRows", False)),
        quota=bool(corps.get("returnPropertyQuota", False)),
        devise=str(corps.get("currencyCode") or CURRENCY_CODE),
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
    # Le filtre voit TOUTES les dimensions citées, le groupement seulement
    # celles qui sont demandées.
    extracteurs = [(d.api_name, d.extraire) for d in requete.toutes_dimensions]
    groupantes = [d.api_name for d in requete.dimensions]
    for plage in requete.plages:
        global_plage = globaux.setdefault(plage.nom, Accumulateur())
        for jour in _jours(plage):
            for unite in _unites(requete, jour):
                valeurs = {nom: extraire(unite) for nom, extraire in extracteurs}
                # dimensionFilter s'applique AVANT toute agrégation : le global
                # de plage (donc les totals) reflète le filtre, comme le vrai
                # service.
                if requete.pred_dimensions and not requete.pred_dimensions(valeurs):
                    continue
                cle = (*(valeurs[nom] for nom in groupantes), plage.nom)
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
    """Les métriques DEMANDÉES puis celles que seul le metricFilter cite : le
    having peut porter sur une métrique absente du rapport, les valeurs
    surnuméraires sont calculées puis jetées à la sérialisation."""
    toutes = [*requete.metriques, *requete.metriques_filtre]
    lignes = []
    for cle, acc in groupes.items():
        valeurs: list[float | int] = [m.calculer(acc) for m in toutes]
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
    """`orderBys` s'ils sont fournis, sinon première métrique DÉCROISSANTE.

    L'ordre par défaut est ATTESTÉ : sur le service réel, un rapport sans
    `orderBys` sort trié par la première métrique décroissante. Le tri
    secondaire (dimensions croissantes) reste un choix du mock — il garantit un
    ordre déterministe là où le vendeur n'en promet aucun. Sans métrique, seules
    les dimensions ordonnent."""
    if not requete.tris:
        if requete.metriques:
            lignes.sort(key=lambda ligne: (-float(ligne.valeurs[0]), ligne.dims, ligne.plage))
        else:
            lignes.sort(key=lambda ligne: (ligne.dims, ligne.plage))
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
    """Toujours une CHAÎNE — la règle proto3-JSON qui surprend tout le monde.

    Et pour les doubles, c'est `DoubleToBuffer` de protobuf, pas le `repr` de
    Python : on tente `%.15g`, et SEULEMENT s'il ne fait pas l'aller-retour on
    passe à `%.17g` — jamais 16. La différence est visible à l'œil nu
    (`0.97348484848484851` chez le vendeur, `0.9734848484848485` avec `repr`),
    et un consommateur qui compare des chaînes la voit. Règle vérifiée sur 21
    valeurs relevées.
    """
    if type_metrique == TYPE_INTEGER:
        return str(int(valeur))
    nombre = float(valeur)
    if nombre.is_integer():
        return str(int(nombre))
    court = format(nombre, ".15g")
    return court if float(court) == nombre else format(nombre, ".17g")


def _valeurs_metriques(requete: Requete, valeurs: list[float | int]) -> list[dict[str, str]]:
    """Les métriques DEMANDÉES seulement : celles ajoutées pour le having sont
    calculées en fin de liste et ne sortent jamais."""
    return [
        {"value": _valeur_metrique(v, m.type_metrique)}
        for v, m in zip(valeurs, requete.metriques, strict=False)
    ]


def _ligne_json(requete: Requete, ligne: Ligne) -> dict[str, Any]:
    dims = [{"value": v} for v in ligne.dims]
    if requete.multi_plages:
        dims.append({"value": ligne.plage})
    corps: dict[str, Any] = {}
    if dims:
        corps["dimensionValues"] = dims
    # Un rapport sans métrique rend des lignes de dimensions NUES : la clé
    # `metricValues` est absente, pas vide (proto3).
    if requete.metriques:
        corps["metricValues"] = _valeurs_metriques(requete, ligne.valeurs)
    return corps


def _lignes_agregations(
    requete: Requete, globaux: dict[str, Accumulateur], lignes: list[Ligne]
) -> dict[str, list[dict[str, Any]]]:
    """totals/maximums/minimums — une ligne par plage.

    TOTAL vient de l'accumulateur GLOBAL de la plage (dédoublonnage exact des
    utilisateurs, là où une somme de lignes sur-compterait), calculé AVANT le
    filtre de métriques — interaction réelle non attestée, UNVERIFIED.
    MAXIMUM/MINIMUM viennent des lignes finales. Les marqueurs
    `RESERVED_TOTAL` / `RESERVED_MAX` / `RESERVED_MIN`, eux, sont ATTESTÉS.
    """
    resultat: dict[str, list[dict[str, Any]]] = {}
    if not requete.agregations or not lignes or not requete.metriques:
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
            ligne_json["metricValues"] = _valeurs_metriques(requete, valeurs)
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
    if requete.metriques:
        corps["metricHeaders"] = [
            {"name": m.api_name, "type": m.type_metrique} for m in requete.metriques
        ]
    if page:
        corps["rows"] = [_ligne_json(requete, ligne) for ligne in page]
    corps.update(_lignes_agregations(requete, globaux, lignes))
    if total:
        corps["rowCount"] = total
    # `currencyCode` de la requête est RENVOYÉ tel quel quand il est fourni —
    # relevé : le champ de réponse fait écho au champ de requête.
    corps["metadata"] = {"currencyCode": requete.devise, "timeZone": TIME_ZONE}
    corps["kind"] = "analyticsData#runReport"
    return corps


# ── Quota ────────────────────────────────────────────────────────────────────


def _bucket(consomme: int, restant: int) -> dict[str, int]:
    """Un QuotaStatus rend TOUJOURS ses deux champs, `consumed: 0` compris.

    C'est l'exception à la règle proto3 d'omission des scalaires par défaut, et
    elle est relevée sur le service : `concurrentRequests` sort bien
    `{"consumed": 0, "remaining": 10}`. Le mock omettait le zéro — un
    consommateur y aurait lu une absence de seau.
    """
    return {"consumed": consomme, "remaining": restant}


def cout_jetons(requete: Requete) -> int:
    """Le coût d'un rapport, INTERPOLÉ sur sept mesures réelles.

    Ce n'est pas un forfait : 1 jeton pour un rapport court et étroit, 7 pour
    la même chose sur 365 jours, 4 pour 9 dimensions par 10 métriques sur 30
    jours. Le modèle reproduit les sept points relevés (cf.
    docs/CONFORMITE-REELLE.md) ; la formule du vendeur, elle, reste inconnue —
    consigné sous `quota-cout-forfaitaire`.
    """
    jours = max((plage.fin - plage.debut).days + 1 for plage in requete.plages)
    cellules = len(requete.dimensions) * len(requete.metriques)
    surcout_cellules = max(0, -(-(cellules - COUT_CELLULES_FRANCHISE) // COUT_CELLULES_PAR_JETON))
    return COUT_JETONS_BASE + jours // COUT_JOURS_PAR_JETON + surcout_cellules


def _bloc_quota(cout: int) -> dict[str, Any]:
    return {
        "tokensPerDay": _bucket(
            cout,
            max(0, settings.quota_tokens_per_day - state.quota_jour_consomme),
        ),
        "tokensPerHour": _bucket(
            cout,
            max(0, settings.quota_tokens_per_hour - state.quota_heure_consomme),
        ),
        # Seau à part entière chez le vendeur (35 % de l'horaire) : l'omettre
        # ferait croire à un consommateur qu'il n'existe que deux plafonds de
        # jetons, alors que c'est CELUI-CI qui l'arrête en premier.
        "tokensPerProjectPerHour": _bucket(
            cout,
            max(
                0,
                settings.quota_tokens_per_project_per_hour - state.quota_projet_heure_consomme,
            ),
        ),
        "concurrentRequests": _bucket(0, 10),
        "serverErrorsPerProjectPerHour": _bucket(0, 10),
        "potentiallyThresholdedRequestsPerHour": _bucket(0, 120),
    }


# ── Le pipeline assemblé ─────────────────────────────────────────────────────


def executer_run_report(corps: dict[str, Any]) -> dict[str, Any]:
    """Peut lever ErreurRequete (→ 400) ou ErreurQuota (→ 429) — la conversion
    en enveloppe HTTP appartient à app.py, ce qui permet à batchRunReports de
    réutiliser le pipeline sous-rapport par sous-rapport."""
    requete = valider(corps)
    # Le quota se consomme APRÈS validation : une requête invalide ne coûte
    # rien, comme chez Google. Le coût dépend de la requête (cf. cout_jetons).
    cout = cout_jetons(requete)
    state.consommer_quota(cout)
    groupes, globaux = collecter(requete)
    if requete.lignes_vides:
        _spine(requete, groupes)
    lignes = calculer_lignes(requete, groupes)
    if requete.pred_metriques:
        # Le having voit les métriques demandées ET celles ajoutées pour lui.
        noms_mets = [m.api_name for m in (*requete.metriques, *requete.metriques_filtre)]
        lignes = [
            ligne
            for ligne in lignes
            if requete.pred_metriques(dict(zip(noms_mets, ligne.valeurs, strict=True)))
        ]
    ordonner(requete, lignes)
    reponse = serialiser(requete, lignes, globaux)
    if requete.quota:
        reponse["propertyQuota"] = _bloc_quota(cout)
    return reponse

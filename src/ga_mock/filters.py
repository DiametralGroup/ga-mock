"""Évaluateur de FilterExpression v1beta.

Arbre récursif : `andGroup` / `orGroup` / `notExpression` / `filter` — un nœud
porte EXACTEMENT un de ces quatre champs. La feuille (`filter`) porte
`fieldName` + un des quatre prédicats : `stringFilter` (matchType,
caseSensitive défaut false), `inListFilter`, `numericFilter` (int64Value en
CHAÎNE ou doubleValue), `betweenFilter` (bornes incluses).

La compilation se fait à la validation : une expression invalide doit sortir
en 400 AVANT toute agrégation, comme chez Google — pas au milieu du calcul.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

Valeurs = dict[str, Any]
Predicat = Callable[[Valeurs], bool]


class ErreurFiltre(Exception):
    """Expression invalide → 400 INVALID_ARGUMENT."""


def _nombre(brut: dict[str, Any]) -> float:
    """NumericValue proto : int64Value arrive en CHAÎNE (règle proto3-JSON),
    doubleValue en nombre. Les deux formes sont acceptées."""
    if "int64Value" in brut:
        try:
            return float(int(str(brut["int64Value"])))
        except ValueError as exc:
            raise ErreurFiltre("Invalid int64Value in numeric filter.") from exc
    if "doubleValue" in brut:
        try:
            return float(brut["doubleValue"])
        except (TypeError, ValueError) as exc:
            raise ErreurFiltre("Invalid doubleValue in numeric filter.") from exc
    raise ErreurFiltre("A numeric filter value must set int64Value or doubleValue.")


def _en_nombre(valeur: Any) -> float:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return 0.0


def _predicat_chaine(filtre: dict[str, Any]) -> Callable[[Any], bool]:
    valeur = str(filtre.get("value", ""))
    sensible = bool(filtre.get("caseSensitive", False))
    match_type = filtre.get("matchType", "EXACT")
    if match_type in ("FULL_REGEXP", "PARTIAL_REGEXP"):
        try:
            motif = re.compile(valeur, 0 if sensible else re.IGNORECASE)
        except re.error as exc:
            raise ErreurFiltre(f"Invalid regular expression: {valeur}.") from exc
        if match_type == "FULL_REGEXP":
            return lambda v: motif.fullmatch(str(v)) is not None
        return lambda v: motif.search(str(v)) is not None

    def normaliser(v: Any) -> str:
        texte = str(v)
        return texte if sensible else texte.casefold()

    reference = normaliser(valeur)
    operations: dict[str, Callable[[Any], bool]] = {
        "EXACT": lambda v: normaliser(v) == reference,
        "BEGINS_WITH": lambda v: normaliser(v).startswith(reference),
        "ENDS_WITH": lambda v: normaliser(v).endswith(reference),
        "CONTAINS": lambda v: reference in normaliser(v),
    }
    if match_type not in operations:
        raise ErreurFiltre(f"Invalid string filter match type: {match_type}.")
    return operations[match_type]


_OPERATIONS_NUMERIQUES: dict[str, Callable[[float, float], bool]] = {
    "EQUAL": lambda v, ref: v == ref,
    "LESS_THAN": lambda v, ref: v < ref,
    "LESS_THAN_OR_EQUAL": lambda v, ref: v <= ref,
    "GREATER_THAN": lambda v, ref: v > ref,
    "GREATER_THAN_OR_EQUAL": lambda v, ref: v >= ref,
}


def _feuille(filtre: dict[str, Any], champs: list[str], sorte: str) -> Predicat:
    nom = filtre.get("fieldName")
    if nom not in champs:
        # Contrainte documentée du vrai service : on ne filtre que sur les
        # champs demandés dans le rapport.
        raise ErreurFiltre(f"Filter field {nom} must be a requested {sorte}.")

    if "stringFilter" in filtre:
        predicat = _predicat_chaine(filtre["stringFilter"])
        return lambda valeurs: predicat(valeurs[nom])
    if "inListFilter" in filtre:
        brut = filtre["inListFilter"]
        sensible = bool(brut.get("caseSensitive", False))
        admises = {str(v) if sensible else str(v).casefold() for v in brut.get("values", [])}
        if sensible:
            return lambda valeurs: str(valeurs[nom]) in admises
        return lambda valeurs: str(valeurs[nom]).casefold() in admises
    if "numericFilter" in filtre:
        brut = filtre["numericFilter"]
        operation = _OPERATIONS_NUMERIQUES.get(brut.get("operation", ""))
        if operation is None:
            raise ErreurFiltre(f"Invalid numeric filter operation: {brut.get('operation')}.")
        reference = _nombre(brut.get("value", {}))
        return lambda valeurs: operation(_en_nombre(valeurs[nom]), reference)
    if "betweenFilter" in filtre:
        brut = filtre["betweenFilter"]
        borne_basse = _nombre(brut.get("fromValue", {}))
        borne_haute = _nombre(brut.get("toValue", {}))
        return lambda valeurs: borne_basse <= _en_nombre(valeurs[nom]) <= borne_haute
    raise ErreurFiltre(
        "A filter must set one of stringFilter, inListFilter, numericFilter or betweenFilter."
    )


def compiler(expression: dict[str, Any], champs: list[str], sorte: str) -> Predicat:
    """Compile l'arbre en fermeture. `champs` = les noms demandés dans le
    rapport (dimensions pour dimensionFilter, métriques pour metricFilter)."""
    if not isinstance(expression, dict):
        raise ErreurFiltre("Invalid FilterExpression.")
    presents = [
        cle for cle in ("andGroup", "orGroup", "notExpression", "filter") if cle in expression
    ]
    if len(presents) != 1:
        raise ErreurFiltre(
            "A FilterExpression must set exactly one of andGroup, orGroup, notExpression or filter."
        )
    cle = presents[0]
    if cle == "filter":
        return _feuille(expression["filter"], champs, sorte)
    if cle == "notExpression":
        interne = compiler(expression["notExpression"], champs, sorte)
        return lambda valeurs: not interne(valeurs)
    sous_expressions = expression[cle].get("expressions", [])
    if not isinstance(sous_expressions, list) or not sous_expressions:
        raise ErreurFiltre(f"{cle} must contain at least one expression.")
    compiles = [compiler(sous, champs, sorte) for sous in sous_expressions]
    if cle == "andGroup":
        return lambda valeurs: all(p(valeurs) for p in compiles)
    return lambda valeurs: any(p(valeurs) for p in compiles)

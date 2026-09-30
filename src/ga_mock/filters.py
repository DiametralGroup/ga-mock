"""FilterExpression v1beta evaluator.

Recursive tree: `andGroup` / `orGroup` / `notExpression` / `filter` — a node
carries EXACTLY one of these four fields. The leaf (`filter`) carries
`fieldName` plus one of five predicates: `stringFilter` (matchType,
caseSensitive defaults to false), `inListFilter`, `numericFilter` (int64Value
as a STRING or doubleValue), `betweenFilter` (bounds inclusive) and
`emptyFilter` (EMPTY proto message — the `{}` is significant, not an
oversight).

Compilation happens at validation time: an invalid expression must come out
as 400 BEFORE any aggregation, like at Google — not partway through the
computation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

Values = dict[str, Any]
Predicate = Callable[[Values], bool]


class FilterError(Exception):
    """Invalid expression → 400 INVALID_ARGUMENT."""


def _number(raw: dict[str, Any]) -> float:
    """Proto NumericValue: int64Value arrives as a STRING (proto3-JSON rule),
    doubleValue as a number. Both forms are accepted."""
    if "int64Value" in raw:
        try:
            return float(int(str(raw["int64Value"])))
        except ValueError as exc:
            raise FilterError("Invalid int64Value in numeric filter.") from exc
    if "doubleValue" in raw:
        try:
            return float(raw["doubleValue"])
        except (TypeError, ValueError) as exc:
            raise FilterError("Invalid doubleValue in numeric filter.") from exc
    raise FilterError("A numeric filter value must set int64Value or doubleValue.")


def _to_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string_predicate(filt: dict[str, Any]) -> Callable[[Any], bool]:
    value = str(filt.get("value", ""))
    sensitive = bool(filt.get("caseSensitive", False))
    match_type = filt.get("matchType", "EXACT")
    if match_type in ("FULL_REGEXP", "PARTIAL_REGEXP"):
        try:
            pattern = re.compile(value, 0 if sensitive else re.IGNORECASE)
        except re.error as exc:
            raise FilterError(f"Invalid regular expression: {value}.") from exc
        if match_type == "FULL_REGEXP":
            return lambda v: pattern.fullmatch(str(v)) is not None
        return lambda v: pattern.search(str(v)) is not None

    def normalize(v: Any) -> str:
        text = str(v)
        return text if sensitive else text.casefold()

    reference = normalize(value)
    operations: dict[str, Callable[[Any], bool]] = {
        "EXACT": lambda v: normalize(v) == reference,
        "BEGINS_WITH": lambda v: normalize(v).startswith(reference),
        "ENDS_WITH": lambda v: normalize(v).endswith(reference),
        "CONTAINS": lambda v: reference in normalize(v),
    }
    if match_type not in operations:
        raise FilterError(f"Invalid string filter match type: {match_type}.")
    return operations[match_type]


# "A filter for empty values such as "(not set)" and "" values" (v1beta
# reference). The empty string and `(not set)` are explicitly named; GA's
# other parenthesized markers (`(none)`, `(direct)`…) denote REAL values, not
# absences — including them would silence rows the real service returns.
# Exact set recorded as unattested.
EMPTY_VALUES = frozenset({"", "(not set)"})

_NUMERIC_OPERATIONS: dict[str, Callable[[float, float], bool]] = {
    "EQUAL": lambda v, ref: v == ref,
    "LESS_THAN": lambda v, ref: v < ref,
    "LESS_THAN_OR_EQUAL": lambda v, ref: v <= ref,
    "GREATER_THAN": lambda v, ref: v > ref,
    "GREATER_THAN_OR_EQUAL": lambda v, ref: v >= ref,
}


def _leaf(filt: dict[str, Any], fields: list[str], kind: str) -> Predicate:
    name = filt.get("fieldName")
    if name not in fields:
        # Documented constraint of the real service: filtering only works on
        # fields requested in the report.
        raise FilterError(f"Filter field {name} must be a requested {kind}.")

    if "stringFilter" in filt:
        predicate = _string_predicate(filt["stringFilter"])
        return lambda values: predicate(values[name])
    if "inListFilter" in filt:
        raw = filt["inListFilter"]
        sensitive = bool(raw.get("caseSensitive", False))
        allowed = {str(v) if sensitive else str(v).casefold() for v in raw.get("values", [])}
        if sensitive:
            return lambda values: str(values[name]) in allowed
        return lambda values: str(values[name]).casefold() in allowed
    if "numericFilter" in filt:
        raw = filt["numericFilter"]
        operation = _NUMERIC_OPERATIONS.get(raw.get("operation", ""))
        if operation is None:
            raise FilterError(f"Invalid numeric filter operation: {raw.get('operation')}.")
        reference = _number(raw.get("value", {}))
        return lambda values: operation(_to_number(values[name]), reference)
    if "betweenFilter" in filt:
        raw = filt["betweenFilter"]
        lower_bound = _number(raw.get("fromValue", {}))
        upper_bound = _number(raw.get("toValue", {}))
        return lambda values: lower_bound <= _to_number(values[name]) <= upper_bound
    if "emptyFilter" in filt:
        # EMPTY proto message: `{}` is the normal payload, there's nothing to
        # validate in it — and a stray `{"x": 1}` must not reject it.
        return lambda values: str(values[name]) in EMPTY_VALUES
    raise FilterError(
        "A filter must set one of stringFilter, inListFilter, numericFilter, "
        "betweenFilter or emptyFilter."
    )


def compile_filter(expression: dict[str, Any], fields: list[str], kind: str) -> Predicate:
    """Compiles the tree into a closure. `fields` = the names requested in
    the report (dimensions for dimensionFilter, metrics for metricFilter)."""
    if not isinstance(expression, dict):
        raise FilterError("Invalid FilterExpression.")
    present = [
        key for key in ("andGroup", "orGroup", "notExpression", "filter") if key in expression
    ]
    if len(present) != 1:
        raise FilterError(
            "A FilterExpression must set exactly one of andGroup, orGroup, notExpression or filter."
        )
    key = present[0]
    if key == "filter":
        return _leaf(expression["filter"], fields, kind)
    if key == "notExpression":
        inner = compile_filter(expression["notExpression"], fields, kind)
        return lambda values: not inner(values)
    sub_expressions = expression[key].get("expressions", [])
    if not isinstance(sub_expressions, list) or not sub_expressions:
        raise FilterError(f"{key} must contain at least one expression.")
    compiled = [compile_filter(sub, fields, kind) for sub in sub_expressions]
    if key == "andGroup":
        return lambda values: all(p(values) for p in compiled)
    return lambda values: any(p(values) for p in compiled)

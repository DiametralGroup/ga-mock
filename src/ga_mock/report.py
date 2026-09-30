"""The runReport pipeline — the heart of the v1beta dialect.

Steps, in the real service's order: validation → date resolution (VIRTUAL
clock) → materialization of visible days → optional fan-out
(pagePath/eventName) → dimension filter → grouping → metric computation →
metric filter (having) → sort → aggregations → pagination → proto3-JSON
serialization (empty repeated fields OMITTED, metric values as STRINGS,
int64 as strings, int32 as numbers).

Each step is a small function: the pipeline reads top to bottom in
`execute_run_report`, and no step can be bypassed by a route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .clock import resolve_date
from .errors import detail_bad_request
from .filters import FilterError, Predicate, compile_filter
from .registry import (
    DIMENSIONS,
    METRICS,
    TYPE_INTEGER,
    Accumulator,
    Dimension,
    Metric,
    Unit,
)
from .settings import CURRENCY_CODE, TIME_ZONE, settings
from .state import state

DEFAULT_LIMIT = 10_000
MAX_LIMIT = 250_000
MAX_DIMENSIONS = 9
MAX_METRICS = 10
MAX_RANGES = 4
SUPPORTED_AGGREGATIONS = ("TOTAL", "MAXIMUM", "MINIMUM")
# `COUNT` is in the proto enum but the service REFUSES it (recorded);
# `METRIC_AGGREGATION_UNSPECIFIED` is the zero value, with no effect.
UNUSED_AGGREGATION = "METRIC_AGGREGATION_UNSPECIFIED"
KNOWN_AGGREGATIONS = (*SUPPORTED_AGGREGATIONS, "COUNT", UNUSED_AGGREGATION)
# Proto switches explicitly refused rather than silently ignored: a consumer
# sending them would otherwise believe they had an effect.
UNSUPPORTED_FIELDS = ("cohortSpec", "comparisons")

# The REAL field set of RunReportRequest. Everything else is refused by the
# vendor's JSON transcoding layer BEFORE the method ever sees the request — a
# `dateRange` instead of `dateRanges` must break HERE.
KNOWN_FIELDS = frozenset(
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
# Violation paths are in snake_case: these are PROTO field names, not the
# request's JSON keys.
NESTED_FIELDS = {
    "dateRanges": ("date_ranges", {"startDate", "endDate", "name"}),
    "dimensions": ("dimensions", {"name", "dimensionExpression"}),
    "metrics": ("metrics", {"name", "expression", "invisible"}),
}

# Service date bounds — CONSTANTS, not the property's creation date:
# `2015-08-13` is refused, `2015-08-14` passes.
DATE_MIN = date(2015, 8, 13)
DATE_MAX = date(3000, 1, 1)

URL_SCHEMA = "https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema"

DIMENSION_RANGE = "dateRange"

# Quota token cost. The real cost is NOT flat: it grows with the range's
# span and the number of cells (dimensions x metrics). Model calibrated on
# seven measurements recorded against a real property — cf.
# docs/CONFORMITE-REELLE.md; this is an INTERPOLATION, not Google's formula
# (recorded: quota-cout-forfaitaire).
BASE_TOKEN_COST = 1
DAYS_PER_TOKEN = 60
FREE_CELLS = 3
CELLS_PER_TOKEN = 32


class RequestError(Exception):
    """Invalid request → 400 INVALID_ARGUMENT, vendor-style message.

    `details` carries the `google.rpc.BadRequest` that the JSON transcoding
    layer attaches to ITS OWN errors (unknown field, invalid enum value) —
    errors from the method itself don't carry one.
    """

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True, slots=True)
class Range:
    name: str
    start: date
    end: date


@dataclass(slots=True)
class ReportQuery:
    """The runReport request once validated — no `dict` beyond this point."""

    dimensions: list[Dimension]
    metrics: list[Metric]
    ranges: list[Range]
    limit: int
    offset: int
    filter_dimensions: list[Dimension] = field(default_factory=list)
    filter_metrics: list[Metric] = field(default_factory=list)
    order_bys: list[dict[str, Any]] = field(default_factory=list)
    dimension_predicate: Predicate | None = None
    metric_predicate: Predicate | None = None
    aggregations: list[str] = field(default_factory=list)
    keep_empty_rows: bool = False
    return_quota: bool = False
    currency: str = CURRENCY_CODE

    @property
    def multi_range(self) -> bool:
        return len(self.ranges) > 1

    @property
    def all_dimensions(self) -> list[Dimension]:
        """The requested ones THEN those cited only by the filter: fan-out
        and extraction must cover both, grouping only the former."""
        return [*self.dimensions, *self.filter_dimensions]

    @property
    def fan_page(self) -> bool:
        return any(d.scope == "page" for d in self.all_dimensions)

    @property
    def fan_event(self) -> bool:
        return any(d.scope == "event" for d in self.all_dimensions)


@dataclass(slots=True)
class Row:
    dims: tuple[str, ...]  # regular dimension values
    range_name: str  # range name (dateRange label)
    values: list[float | int] = field(default_factory=list)


# ── Validation ───────────────────────────────────────────────────────────────


def invalid_field(name: str, kind: str) -> RequestError:
    """The vendor's message, down to the spacing.

    Yes: ONE space after "dimension.", TWO after "metric.". That's what the
    service writes, and a consumer test comparing the full string must pass
    against the mock. The "Did you mean … ?" suggestion the real service
    prefixes is NOT reproduced (recorded).
    """
    separator = " " if kind == "dimension" else "  "
    return RequestError(
        f"Field {name} is not a valid {kind}.{separator}"
        f"For a list of valid dimensions and metrics, see {URL_SCHEMA} "
    )


def _names(body: dict[str, Any], field_name: str, registry: dict[str, Any], kind: str) -> list[str]:
    raw = body.get(field_name) or []
    if not isinstance(raw, list):
        raise RequestError(f"Invalid value for {field_name}.")
    names: list[str] = []
    for entry in raw:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise RequestError(f"Invalid value for {field_name}.")
        if name not in registry:
            raise invalid_field(name, kind)
        if name in names:
            raise RequestError(
                f"Found duplicate dimensions: {name}"
                if kind == "dimension"
                else f"Duplicate metrics are not allowed. Found duplicate metrics: {name}"
            )
        names.append(name)
    return names


def _unknown_fields(body: dict[str, Any]) -> None:
    """Refusal of unknown JSON keys, BEFORE everything else.

    At the vendor, it's the transcoding layer talking, not the method: the
    request never reaches the API. All faulty keys are listed, one violation
    each, and the message is their concatenation.
    """
    violations: list[tuple[str, str]] = [
        ("", f'Invalid JSON payload received. Unknown name "{key}": Cannot find field.')
        for key in body
        if key not in KNOWN_FIELDS
    ]
    for key, (proto_path, allowed) in NESTED_FIELDS.items():
        entries = body.get(key)
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            violations += [
                (
                    "",
                    f'Invalid JSON payload received. Unknown name "{sub}" '
                    f"at '{proto_path}[{i}]': Cannot find field.",
                )
                for sub in entry
                if sub not in allowed
            ]
    if violations:
        raise RequestError(
            "\n".join(description for _, description in violations),
            detail_bad_request(violations),
        )


def _int64(body: dict[str, Any], field_name: str, default: int) -> int:
    """proto3 int64: accepted as a JSON number OR a string — both forms are
    legal on the wire, a client must not be punished for either."""
    raw = body.get(field_name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise RequestError(f"Invalid value for {field_name}.")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        digits = text[1:] if text.startswith("-") else text
        if digits.isdigit():
            return int(text)
    raise RequestError(f"Invalid value for {field_name}.")


def _date_bound(proto_name: str, value: date) -> None:
    if not (DATE_MIN < value < DATE_MAX):
        raise RequestError(
            f"{proto_name} = {value.isoformat()} must be greater than "
            f"{DATE_MIN.isoformat()} and less than {DATE_MAX.isoformat()}."
        )


def _ranges(body: dict[str, Any]) -> list[Range]:
    raw = body.get("dateRanges")
    if not isinstance(raw, list) or not raw:
        raise RequestError("A dateRange is required.")
    if len(raw) > MAX_RANGES:
        raise RequestError(
            f"Requests are limited to {MAX_RANGES} dateRanges.\n"
            f"  This request contains {len(raw)} dateRanges."
        )
    ranges: list[Range] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RequestError("Invalid value for dateRanges.")
        bounds: list[date] = []
        for key in ("startDate", "endDate"):
            text = str(entry.get(key, ""))
            try:
                bounds.append(resolve_date(text))
            except ValueError as exc:
                raise RequestError(
                    f"Invalid {key} : {text}. {key} must be YYYY-MM-DD, "
                    "NdaysAgo, yesterday, or today."
                ) from exc
        start, end = bounds
        # Check order matches the service's: fixed bounds BEFORE
        # start/end consistency (a reversed range outside the bounds fails
        # on the bound, not on the reversal).
        _date_bound("start_date", start)
        _date_bound("end_date", end)
        if start > end:
            raise RequestError(
                "start_date must be less than or equal to end_date. "
                f"start_date = {start.isoformat()} and end_date = {end.isoformat()}"
            )
        name = entry.get("name") or f"date_range_{i}"
        ranges.append(Range(str(name), start, end))
    return ranges


def _aggregations(body: dict[str, Any]) -> list[str]:
    """`COUNT` is part of the proto enum but the service REFUSES it; a value
    outside the enum fails earlier, in JSON transcoding — so the two messages
    differ, and both are recorded."""
    raw = body.get("metricAggregations") or []
    if not isinstance(raw, list):
        raise RequestError("Invalid value for metricAggregations.")
    for i, a in enumerate(raw):
        if a not in KNOWN_AGGREGATIONS:
            field_path = f"metric_aggregations[{i}]"
            violation = (
                f"Invalid value at '{field_path}' "
                "(type.googleapis.com/google.analytics.data.v1beta.MetricAggregation), "
                f'"{a}"'
            )
            raise RequestError(violation, detail_bad_request([(field_path, violation)]))
        if a == "COUNT":
            raise RequestError("Metric aggregation Count is not supported in ReportRequest.")
    return [str(a) for a in raw if a != UNUSED_AGGREGATION]


def _order_bys(body: dict[str, Any], dims: list[str], mets: list[str]) -> list[dict[str, Any]]:
    """An `orderBy` can ONLY target requested fields — a real constraint,
    unlike filters. The message names the faulty field (empty string when the
    entry targets neither a metric nor a dimension)."""
    raw = body.get("orderBys") or []
    if not isinstance(raw, list):
        raise RequestError("Invalid value for orderBys.")
    allowed = {*dims, *mets, DIMENSION_RANGE}
    for order_by in raw:
        if not isinstance(order_by, dict):
            raise RequestError("Invalid value for orderBys.")
        metric, dimension = order_by.get("metric"), order_by.get("dimension")
        target = ""
        if isinstance(metric, dict):
            target = str(metric.get("metricName", ""))
        elif isinstance(dimension, dict):
            target = str(dimension.get("dimensionName", ""))
        if target not in allowed:
            raise RequestError(
                f"Field {target} exists in OrderBy but is not defined in input "
                "Dimensions/Metrics list"
            )
    return [dict(order_by) for order_by in raw]


def _filter_fields(raw: Any) -> list[str]:
    """The `fieldName`s cited by a filter tree, in reading order."""
    fields: list[str] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key == "fieldName" and isinstance(value, str):
                fields.append(value)
            else:
                fields += _filter_fields(value)
    elif isinstance(raw, list):
        for value in raw:
            fields += _filter_fields(value)
    return fields


def _compile_filter_field(raw: Any, kind: str) -> tuple[Predicate | None, list[str]]:
    """Compiles a filter and ALSO returns the fields it cites.

    Recorded from the service: a filter does NOT have to target a field
    requested in the report — filtering on `country` while grouping by `date`
    works. The mock therefore had to stop requiring it, and now extracts the
    dimensions cited by the filter alone.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        raise RequestError(f"Invalid value for {kind}Filter.")
    registry = DIMENSIONS if kind == "dimension" else METRICS
    other = METRICS if kind == "dimension" else DIMENSIONS
    fields = _filter_fields(raw)
    for name in fields:
        if name in registry:
            continue
        # Citing a metric in a dimensionFilter (or the reverse) has its own
        # message at the vendor — two distinct diagnostics.
        if name in other and kind == "dimension":
            raise RequestError("Found duplicate dimensions/metrics.")
        raise invalid_field(name, kind)
    try:
        return compile_filter(raw, fields, kind), fields
    except FilterError as exc:
        raise RequestError(str(exc)) from exc


def validate(body: dict[str, Any]) -> ReportQuery:
    """The ORDER of checks is the service's, recorded case by case.

    Nothing about it is obvious: field NAME validity and `limit` positivity
    come BEFORE the date range requirement, while cardinality bounds (9
    dimensions, 10 metrics) come AFTER. A consumer sending a doubly-faulty
    request must get here the message they'll get in prod, not another one.
    """
    # 1. JSON transcoding: unknown fields, enum values.
    _unknown_fields(body)
    for field_name in UNSUPPORTED_FIELDS:
        if field_name in body:
            raise RequestError(f"{field_name} is not supported by this mock.")
    # 2. Name validity and duplicates.
    metric_names = _names(body, "metrics", METRICS, "metric")
    dimension_names = _names(body, "dimensions", DIMENSIONS, "dimension")
    # 3. Pagination.
    limit = _int64(body, "limit", DEFAULT_LIMIT)
    if limit < 0:
        raise RequestError(f"limit must be positive. The API received limit = {limit}")
    if limit == 0:
        limit = DEFAULT_LIMIT
    offset = _int64(body, "offset", 0)
    if offset < 0:
        raise RequestError(f"offset must be positive. The API received offset = {offset}")
    # 4. Date ranges — BEFORE cardinality bounds: ten dimensions without a
    #    range fail on "A dateRange is required.".
    ranges = _ranges(body)
    # 5. Cardinality bounds.
    if len(metric_names) > MAX_METRICS:
        raise RequestError(
            f"Requests are limited to {MAX_METRICS} metrics within a nested request.\n"
            f"  This request is for {len(metric_names)} metrics."
        )
    if len(dimension_names) > MAX_DIMENSIONS:
        raise RequestError(
            f"Requests are limited to {MAX_DIMENSIONS} dimensions within a nested request.\n"
            f"  This request is for {len(dimension_names)} dimensions."
        )
    # 6. A report WITHOUT metrics is valid: it returns bare dimension rows,
    #    with no `metricHeaders` or `metricValues`. Only the absence of BOTH
    #    is refused.
    if not metric_names and not dimension_names:
        raise RequestError(
            "Requests require dimensions and/or metrics. Most requests include both."
        )
    dimension_predicate, dimension_filter_fields = _compile_filter_field(
        body.get("dimensionFilter"), "dimension"
    )
    metric_predicate, metric_filter_fields = _compile_filter_field(
        body.get("metricFilter"), "metric"
    )
    return ReportQuery(
        dimensions=[DIMENSIONS[n] for n in dimension_names],
        metrics=[METRICS[n] for n in metric_names],
        # Dimensions cited by the filter ALONE: extracted to evaluate it, but
        # never grouped or returned.
        filter_dimensions=[
            DIMENSIONS[n]
            for n in dict.fromkeys(dimension_filter_fields)
            if n not in dimension_names
        ],
        filter_metrics=[
            METRICS[n] for n in dict.fromkeys(metric_filter_fields) if n not in metric_names
        ],
        ranges=ranges,
        # Capped SILENTLY, not rejected: that's the documented behavior.
        limit=min(limit, MAX_LIMIT),
        offset=offset,
        order_bys=_order_bys(body, dimension_names, metric_names),
        dimension_predicate=dimension_predicate,
        metric_predicate=metric_predicate,
        aggregations=_aggregations(body),
        keep_empty_rows=bool(body.get("keepEmptyRows", False)),
        return_quota=bool(body.get("returnPropertyQuota", False)),
        currency=str(body.get("currencyCode") or CURRENCY_CODE),
    )


# ── Collection and grouping ──────────────────────────────────────────────────


def _days(range_: Range) -> list[date]:
    return [range_.start + timedelta(days=i) for i in range((range_.end - range_.start).days + 1)]


def _units(query: ReportQuery, d: date) -> list[Unit]:
    sessions = state.visible_sessions(d)
    if query.fan_page and query.fan_event:
        # Assumed cross product: GA attributes the event to ITS page; the
        # mock doesn't model that link — approximation recorded (UNVERIFIED).
        return [
            Unit(s, page=p, event=name, event_count=n)
            for s in sessions
            for p in s.pages
            for name, n in s.events
        ]
    if query.fan_page:
        return [Unit(s, page=p) for s in sessions for p in s.pages]
    if query.fan_event:
        return [Unit(s, event=name, event_count=n) for s in sessions for name, n in s.events]
    return [Unit(s) for s in sessions]


def collect(
    query: ReportQuery,
) -> tuple[dict[tuple[str, ...], Accumulator], dict[str, Accumulator]]:
    """Groups by key (dim values + range name) + global per range."""
    groups: dict[tuple[str, ...], Accumulator] = {}
    totals: dict[str, Accumulator] = {}
    fan_page, fan_event = query.fan_page, query.fan_event
    # The filter sees ALL cited dimensions, grouping only the requested ones.
    extractors = [(d.api_name, d.extract) for d in query.all_dimensions]
    grouping = [d.api_name for d in query.dimensions]
    for range_ in query.ranges:
        range_total = totals.setdefault(range_.name, Accumulator())
        for day in _days(range_):
            for unit in _units(query, day):
                values = {name: extract(unit) for name, extract in extractors}
                # dimensionFilter applies BEFORE any aggregation: the range's
                # global (hence the totals) reflects the filter, like the
                # real service.
                if query.dimension_predicate and not query.dimension_predicate(values):
                    continue
                key = (*(values[name] for name in grouping), range_.name)
                acc = groups.get(key)
                if acc is None:
                    acc = groups[key] = Accumulator()
                acc.add(unit, fan_page=fan_page, fan_event=fan_event)
                range_total.add(unit, fan_page=fan_page, fan_event=fan_event)
    return groups, totals


def _spine(query: ReportQuery, groups: dict[tuple[str, ...], Accumulator]) -> None:
    """keepEmptyRows: fills in the calendar when ALL dimensions belong to the
    date family — the BI "gap-free time series" case. Other families aren't
    synthesized (documented limitation)."""
    if not query.dimensions or not all(d.from_date for d in query.dimensions):
        return
    for range_ in query.ranges:
        for day in _days(range_):
            key = (
                *(d.from_date(day) if d.from_date else "" for d in query.dimensions),
                range_.name,
            )
            groups.setdefault(key, Accumulator())


def compute_rows(query: ReportQuery, groups: dict[tuple[str, ...], Accumulator]) -> list[Row]:
    """REQUESTED metrics first, then those cited only by metricFilter: having
    can target a metric absent from the report, the extra values are
    computed then discarded at serialization."""
    all_metrics = [*query.metrics, *query.filter_metrics]
    rows = []
    for key, acc in groups.items():
        values: list[float | int] = [m.compute(acc) for m in all_metrics]
        rows.append(Row(dims=key[:-1], range_name=key[-1], values=values))
    return rows


# ── Sorting ──────────────────────────────────────────────────────────────────


def _dimension_key(value: str, order_type: str) -> Any:
    if order_type == "NUMERIC":
        try:
            return float(value)
        except ValueError:
            return 0.0
    if order_type == "CASE_INSENSITIVE_ALPHANUMERIC":
        return value.casefold()
    return value


def order(query: ReportQuery, rows: list[Row]) -> None:
    """`orderBys` if provided, otherwise the first metric DESCENDING.

    The default order is ATTESTED: on the real service, a report without
    `orderBys` comes out sorted by the first metric descending. The secondary
    sort (dimensions ascending) is a mock choice — it guarantees a
    deterministic order where the vendor promises none. Without a metric,
    only dimensions order the rows."""
    if not query.order_bys:
        if query.metrics:
            rows.sort(key=lambda row: (-float(row.values[0]), row.dims, row.range_name))
        else:
            rows.sort(key=lambda row: (row.dims, row.range_name))
        return
    dim_names = [d.api_name for d in query.dimensions]
    metric_names = [m.api_name for m in query.metrics]
    # Stable sorts applied from last to first: the first orderBy dominates.
    for order_by in reversed(query.order_bys):
        desc = bool(order_by.get("desc", False))
        if order_by.get("metric") is not None:
            index = metric_names.index(order_by["metric"]["metricName"])

            def by_metric(row: Row, i: int = index) -> float:
                return float(row.values[i])

            rows.sort(key=by_metric, reverse=desc)
        else:
            name = order_by["dimension"]["dimensionName"]
            order_type = order_by["dimension"].get("orderType", "ALPHANUMERIC")
            if name == DIMENSION_RANGE:
                rows.sort(key=lambda row: row.range_name, reverse=desc)
            else:
                index = dim_names.index(name)

                def by_dimension(row: Row, i: int = index, t: str = order_type) -> Any:
                    return _dimension_key(row.dims[i], t)

                rows.sort(key=by_dimension, reverse=desc)


# ── Serialization ────────────────────────────────────────────────────────────


def _metric_value(value: float | int, metric_type: str) -> str:
    """Always a STRING — the proto3-JSON rule that surprises everyone.

    And for doubles, it's protobuf's `DoubleToBuffer`, not Python's `repr`:
    try `%.15g` first, and ONLY fall back to `%.17g` if it doesn't round-trip
    — never 16. The difference is visible to the naked eye
    (`0.97348484848484851` at the vendor, `0.9734848484848485` with `repr`),
    and a consumer comparing strings sees it. Rule verified against 21
    recorded values.
    """
    if metric_type == TYPE_INTEGER:
        return str(int(value))
    number = float(value)
    if number.is_integer():
        return str(int(number))
    short = format(number, ".15g")
    return short if float(short) == number else format(number, ".17g")


def _metric_values(query: ReportQuery, values: list[float | int]) -> list[dict[str, str]]:
    """REQUESTED metrics only: those added for having are computed at the end
    of the list and never come out."""
    return [
        {"value": _metric_value(v, m.metric_type)}
        for v, m in zip(values, query.metrics, strict=False)
    ]


def _row_json(query: ReportQuery, row: Row) -> dict[str, Any]:
    dims = [{"value": v} for v in row.dims]
    if query.multi_range:
        dims.append({"value": row.range_name})
    body: dict[str, Any] = {}
    if dims:
        body["dimensionValues"] = dims
    # A report without metrics returns BARE dimension rows: the
    # `metricValues` key is absent, not empty (proto3).
    if query.metrics:
        body["metricValues"] = _metric_values(query, row.values)
    return body


def _aggregation_rows(
    query: ReportQuery, totals: dict[str, Accumulator], rows: list[Row]
) -> dict[str, list[dict[str, Any]]]:
    """totals/maximums/minimums — one row per range.

    TOTAL comes from the range's GLOBAL accumulator (exact user
    deduplication, where a sum of rows would overcount), computed BEFORE the
    metric filter — real interaction, unattested, UNVERIFIED.
    MAXIMUM/MINIMUM come from the final rows. The `RESERVED_TOTAL` /
    `RESERVED_MAX` / `RESERVED_MIN` markers, though, are ATTESTED.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    if not query.aggregations or not rows or not query.metrics:
        return result
    markers = {"TOTAL": "RESERVED_TOTAL", "MAXIMUM": "RESERVED_MAX", "MINIMUM": "RESERVED_MIN"}
    keys = {"TOTAL": "totals", "MAXIMUM": "maximums", "MINIMUM": "minimums"}
    for aggregation in query.aggregations:
        agg_rows: list[dict[str, Any]] = []
        for range_ in query.ranges:
            if aggregation == "TOTAL":
                acc = totals[range_.name]
                values = [m.compute(acc) for m in query.metrics]
            else:
                candidates = [row.values for row in rows if row.range_name == range_.name] or [
                    [0] * len(query.metrics)
                ]
                selector = max if aggregation == "MAXIMUM" else min
                values = [
                    selector(float(v[i]) for v in candidates) for i in range(len(query.metrics))
                ]
            dims = [{"value": markers[aggregation]} for _ in query.dimensions]
            if query.multi_range:
                dims.append({"value": range_.name})
            row_json: dict[str, Any] = {}
            if dims:
                row_json["dimensionValues"] = dims
            row_json["metricValues"] = _metric_values(query, values)
            agg_rows.append(row_json)
        result[keys[aggregation]] = agg_rows
    return result


def serialize(
    query: ReportQuery,
    rows: list[Row],
    totals: dict[str, Accumulator],
) -> dict[str, Any]:
    """Assembles the response, OMITTING empty repeated fields and zero
    int32s — proto3-JSON. No `"rows": []`, no `"rowCount": 0`."""
    total = len(rows)
    page = rows[query.offset : query.offset + query.limit]
    body: dict[str, Any] = {}
    dim_headers = [{"name": d.api_name} for d in query.dimensions]
    if query.multi_range:
        dim_headers.append({"name": DIMENSION_RANGE})
    if dim_headers:
        body["dimensionHeaders"] = dim_headers
    if query.metrics:
        body["metricHeaders"] = [{"name": m.api_name, "type": m.metric_type} for m in query.metrics]
    if page:
        body["rows"] = [_row_json(query, row) for row in page]
    body.update(_aggregation_rows(query, totals, rows))
    if total:
        body["rowCount"] = total
    # The request's `currencyCode` is ECHOED BACK as-is when provided —
    # recorded: the response field mirrors the request field.
    body["metadata"] = {"currencyCode": query.currency, "timeZone": TIME_ZONE}
    body["kind"] = "analyticsData#runReport"
    return body


# ── Quota ────────────────────────────────────────────────────────────────────


def _bucket(consumed: int, remaining: int) -> dict[str, int]:
    """A QuotaStatus ALWAYS returns both its fields, `consumed: 0` included.

    This is the exception to the proto3 default-scalar-omission rule, and
    it's recorded against the service: `concurrentRequests` really does come
    out as `{"consumed": 0, "remaining": 10}`. The mock used to omit the
    zero — a consumer would have read that as a missing bucket.
    """
    return {"consumed": consumed, "remaining": remaining}


def token_cost(query: ReportQuery) -> int:
    """The cost of a report, INTERPOLATED from seven real measurements.

    It isn't a flat rate: 1 token for a short, narrow report, 7 for the same
    thing over 365 days, 4 for 9 dimensions by 10 metrics over 30 days. The
    model reproduces the seven recorded points (cf. docs/CONFORMITE-REELLE.md);
    the vendor's actual formula remains unknown — recorded under
    `quota-cout-forfaitaire`.
    """
    days = max((range_.end - range_.start).days + 1 for range_ in query.ranges)
    cells = len(query.dimensions) * len(query.metrics)
    cell_surcharge = max(0, -(-(cells - FREE_CELLS) // CELLS_PER_TOKEN))
    return BASE_TOKEN_COST + days // DAYS_PER_TOKEN + cell_surcharge


def _quota_block(cost: int) -> dict[str, Any]:
    return {
        "tokensPerDay": _bucket(
            cost,
            max(0, settings.quota_tokens_per_day - state.quota_day_consumed),
        ),
        "tokensPerHour": _bucket(
            cost,
            max(0, settings.quota_tokens_per_hour - state.quota_hour_consumed),
        ),
        # A full-fledged bucket at the vendor (35% of the hourly one):
        # omitting it would make a consumer believe there are only two token
        # caps, when this is the ONE that stops them first.
        "tokensPerProjectPerHour": _bucket(
            cost,
            max(
                0,
                settings.quota_tokens_per_project_per_hour - state.quota_project_hour_consumed,
            ),
        ),
        "concurrentRequests": _bucket(0, 10),
        "serverErrorsPerProjectPerHour": _bucket(0, 10),
        "potentiallyThresholdedRequestsPerHour": _bucket(0, 120),
    }


# ── The assembled pipeline ───────────────────────────────────────────────────


def execute_run_report(body: dict[str, Any]) -> dict[str, Any]:
    """May raise RequestError (→ 400) or QuotaError (→ 429) — converting to
    an HTTP envelope belongs to app.py, which lets batchRunReports reuse the
    pipeline sub-report by sub-report."""
    query = validate(body)
    # Quota is consumed AFTER validation: an invalid request costs nothing,
    # like at Google. The cost depends on the request (cf. token_cost).
    cost = token_cost(query)
    state.consume_quota(cost)
    groups, totals = collect(query)
    if query.keep_empty_rows:
        _spine(query, groups)
    rows = compute_rows(query, groups)
    if query.metric_predicate:
        # having sees both the requested metrics AND those added for it.
        metric_names = [m.api_name for m in (*query.metrics, *query.filter_metrics)]
        rows = [
            row
            for row in rows
            if query.metric_predicate(dict(zip(metric_names, row.values, strict=True)))
        ]
    order(query, rows)
    response = serialize(query, rows, totals)
    if query.return_quota:
        response["propertyQuota"] = _quota_block(cost)
    return response

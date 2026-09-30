"""pydantic models of the CONTRACT — the shape, not the validation.

Handlers return `JSONResponse`: FastAPI therefore documents these models in
the OpenAPI schema WITHOUT revalidating outputs (a mock must be able to serve
deliberately abnormal payloads, injected failures included). Input validation
is done by hand in report.py, in the Google dialect — never by pydantic,
whose 422s would give FastAPI away.

Honesty: any field or behavior unattested against the API's public reference
carries an `x-ga-confidence: unverified` marker (field) or an entry in
UNVERIFIED_BEHAVIORS (behavior). A test enforces that every entry is
documented in docs/UNVERIFIED-FIELDS.md — inventing without saying so is a
build failure, not an opinion.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Approximated behaviors, or ones with unattested wordings. Each identifier
# MUST appear in docs/UNVERIFIED-FIELDS.md (dedicated test). Whatever gets
# RECORDED against the real service leaves here and moves into
# docs/CONFORMITE-REELLE.md — the only move allowed in that direction.
# NOTE: the tags below stay in French — they are cross-referenced verbatim
# against docs/UNVERIFIED-FIELDS.md (outside this translation's scope).
UNVERIFIED_BEHAVIORS: tuple[str, ...] = (
    "suggestions-did-you-mean",
    "raison-parseur-json",
    "ordre-par-defaut-secondaire",
    "regle-semaine",
    "fallback-session-campaign-name",
    "fanout-inter-portees",
    "total-avant-having",
    "cout-jetons-interpole",
    "retry-after-sur-429",
    "aud-tolerant",
    "fenetre-assertion-double-horloge",
    "messages-quota-429",
    "valeurs-vides-emptyfilter",
    "spine-keep-empty-rows",
    "vocabulaire-sans-not-set",
)


def _unverified(note: str) -> dict[str, Any]:
    return {"x-ga-confidence": "unverified", "x-ga-note": note}


# ── Requests ─────────────────────────────────────────────────────────────────


class DateRange(BaseModel):
    startDate: str = Field(description="YYYY-MM-DD, today, yesterday or NdaysAgo.")
    endDate: str = Field(description="YYYY-MM-DD, today, yesterday or NdaysAgo.")
    name: str | None = None


class DimensionSpec(BaseModel):
    name: str


class MetricSpec(BaseModel):
    name: str


class StringFilter(BaseModel):
    matchType: str = "EXACT"
    value: str = ""
    caseSensitive: bool = False


class InListFilter(BaseModel):
    values: list[str] = []
    caseSensitive: bool = False


class NumericValue(BaseModel):
    int64Value: str | None = Field(default=None, description="int64 carried as a string.")
    doubleValue: float | None = None


class NumericFilter(BaseModel):
    operation: str
    value: NumericValue


class BetweenFilter(BaseModel):
    fromValue: NumericValue
    toValue: NumericValue


class EmptyFilter(BaseModel):
    """EMPTY proto message: `{}` is the expected payload, not an oversight."""


class FilterLeaf(BaseModel):
    fieldName: str
    stringFilter: StringFilter | None = None
    inListFilter: InListFilter | None = None
    numericFilter: NumericFilter | None = None
    betweenFilter: BetweenFilter | None = None
    emptyFilter: EmptyFilter | None = Field(
        default=None,
        json_schema_extra=_unverified(
            'matches "" and "(not set)"; the exact placeholder set is not attested'
        ),
    )


class FilterExpressionList(BaseModel):
    expressions: list[FilterExpression] = []


class FilterExpression(BaseModel):
    andGroup: FilterExpressionList | None = None
    orGroup: FilterExpressionList | None = None
    notExpression: FilterExpression | None = None
    filter: FilterLeaf | None = None


class MetricOrderBy(BaseModel):
    metricName: str


class DimensionOrderBy(BaseModel):
    dimensionName: str
    orderType: str = "ALPHANUMERIC"


class OrderBy(BaseModel):
    desc: bool = False
    metric: MetricOrderBy | None = None
    dimension: DimensionOrderBy | None = None


class RunReportRequest(BaseModel):
    dateRanges: list[DateRange] = Field(
        default=[],
        description="1 to 4 ranges; dates must fall between 2015-08-14 and 2999-12-31.",
    )
    dimensions: list[DimensionSpec] = Field(default=[], description="Up to 9.")
    metrics: list[MetricSpec] = Field(
        default=[],
        description="Up to 10. Optional: a dimensions-only report is valid.",
    )
    dimensionFilter: FilterExpression | None = None
    metricFilter: FilterExpression | None = Field(
        default=None, description="Applied AFTER aggregation, on requested metrics."
    )
    limit: int | str | None = Field(
        default=None,
        description=(
            "int64 accepted as number or string; over 250000 silently capped; "
            "0 falls back to the 10000 default; negative is rejected."
        ),
    )
    offset: int | str | None = None
    orderBys: list[OrderBy] = []
    metricAggregations: list[str] = Field(
        default=[],
        description=(
            "TOTAL, MAXIMUM and MINIMUM are served. COUNT is a valid enum value "
            "that the service itself rejects."
        ),
    )
    keepEmptyRows: bool = False
    returnPropertyQuota: bool = False
    currencyCode: str | None = Field(
        default=None, description="ISO 4217; echoed back in metadata.currencyCode."
    )


class BatchRunReportsRequest(BaseModel):
    requests: list[RunReportRequest] = Field(default=[], description="1 to 5 requests.")


# ── Responses ────────────────────────────────────────────────────────────────


class DimensionHeader(BaseModel):
    name: str


class MetricHeader(BaseModel):
    name: str
    type: str


class DimensionValue(BaseModel):
    value: str


class MetricValue(BaseModel):
    value: str = Field(description="Always serialized as a string (proto3 JSON).")


class Row(BaseModel):
    dimensionValues: list[DimensionValue] = Field(
        default=[], description="Omitted entirely when the report has no dimensions."
    )
    metricValues: list[MetricValue] = Field(
        default=[], description="Omitted entirely when the report has no metrics."
    )


class ResponseMetaData(BaseModel):
    currencyCode: str
    timeZone: str


class QuotaStatus(BaseModel):
    consumed: int = Field(description="Always present, zero included — unlike most proto3 ints.")
    remaining: int = Field(description="Always present, zero included.")


class PropertyQuota(BaseModel):
    tokensPerDay: QuotaStatus = Field(
        json_schema_extra=_unverified(
            "token cost is interpolated from seven measurements, not the vendor's formula"
        )
    )
    tokensPerHour: QuotaStatus
    tokensPerProjectPerHour: QuotaStatus = Field(
        description="35% of the hourly token bucket — 14000 for a standard property."
    )
    concurrentRequests: QuotaStatus
    serverErrorsPerProjectPerHour: QuotaStatus
    potentiallyThresholdedRequestsPerHour: QuotaStatus


class RunReportResponse(BaseModel):
    dimensionHeaders: list[DimensionHeader] = []
    metricHeaders: list[MetricHeader] = Field(
        default=[], description="Key ABSENT when the request carries no metrics."
    )
    rows: list[Row] = Field(default=[], description="Key ABSENT when there are no rows.")
    totals: list[Row] = Field(
        default=[],
        json_schema_extra=_unverified(
            "RESERVED_TOTAL markers are attested; computing TOTAL BEFORE metricFilter is not"
        ),
    )
    maximums: list[Row] = []
    minimums: list[Row] = []
    rowCount: int | None = Field(
        default=None, description="Pre-pagination count; key absent when zero."
    )
    metadata: ResponseMetaData
    propertyQuota: PropertyQuota | None = None
    kind: str = "analyticsData#runReport"


class BatchRunReportsResponse(BaseModel):
    reports: list[RunReportResponse] = []
    kind: str = "analyticsData#batchRunReports"


class DimensionMetadata(BaseModel):
    apiName: str
    uiName: str
    description: str
    category: str
    deprecatedApiNames: list[str] = Field(
        default=[], description="Former API names still announced; key absent when none."
    )
    customDefinition: bool | None = Field(
        default=None, description="Key ABSENT when false — never serialized for standard fields."
    )


class MetricMetadata(BaseModel):
    apiName: str
    uiName: str
    description: str
    category: str
    type: str
    deprecatedApiNames: list[str] = Field(
        default=[], description="Former API names still announced; key absent when none."
    )
    customDefinition: bool | None = Field(default=None, description="Key ABSENT when false.")


class ComparisonMetadata(BaseModel):
    apiName: str
    uiName: str
    description: str


class MetadataResponse(BaseModel):
    name: str
    dimensions: list[DimensionMetadata] = []
    metrics: list[MetricMetadata] = []
    comparisons: list[ComparisonMetadata] = Field(
        default=[], description="Stock GA4 comparisons; absent for properties/0."
    )


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int = Field(description="3599: the issuance second is already spent.")
    token_type: str = "Bearer"


class OAuthErrorResponse(BaseModel):
    error: str
    error_description: str


class ErrorStatus(BaseModel):
    code: int
    message: str
    status: str


class ErrorResponse(BaseModel):
    error: ErrorStatus


# Error responses documented on every route of the Data surface.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "INVALID_ARGUMENT"},
    401: {"model": ErrorResponse, "description": "UNAUTHENTICATED"},
    403: {"model": ErrorResponse, "description": "PERMISSION_DENIED"},
    429: {"model": ErrorResponse, "description": "RESOURCE_EXHAUSTED"},
}


def request_body(model: type[BaseModel]) -> dict[str, Any]:
    """Self-contained OpenAPI 3.1 `requestBody` block (local $defs): handlers
    keep their raw `Request` — documenting isn't validating."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }

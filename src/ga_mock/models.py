"""Modèles pydantic du CONTRAT — la forme, pas la validation.

Les handlers rendent des `JSONResponse` : FastAPI documente donc ces modèles
dans l'OpenAPI SANS revalider les sorties (un mock doit pouvoir servir des
charges volontairement anormales, pannes injectées comprises). La validation
des entrées est faite à la main dans report.py, au dialecte Google — jamais
par pydantic, dont les 422 trahiraient FastAPI.

Honnêteté : tout champ ou comportement non attesté contre la référence
publique de l'API porte un marqueur `x-ga-confidence: unverified` (champ) ou
une entrée dans UNVERIFIED_BEHAVIORS (comportement). Un test impose que chaque
entrée soit documentée dans docs/UNVERIFIED-FIELDS.md — inventer sans le dire
est une faute de build, pas une opinion.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Comportements approximés ou aux wordings non attestés hors ligne. Chaque
# identifiant DOIT apparaître dans docs/UNVERIFIED-FIELDS.md (test dédié).
UNVERIFIED_BEHAVIORS: tuple[str, ...] = (
    "messages-erreurs-validation",
    "messages-oauth-token",
    "ordre-par-defaut",
    "regle-semaine",
    "fallback-session-campaign-name",
    "type-keyevents",
    "fanout-inter-portees",
    "marqueurs-reserved",
    "quota-cout-forfaitaire",
    "www-authenticate-realm",
    "enveloppe-404-405",
    "geo-sans-accents",
    "retry-after-sur-429",
    "aud-tolerant",
    "limit-zero-defaut",
    "position-dimension-daterange",
    "fenetre-assertion-double-horloge",
)


def _unverified(note: str) -> dict[str, Any]:
    return {"x-ga-confidence": "unverified", "x-ga-note": note}


# ── Requêtes ─────────────────────────────────────────────────────────────────


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


class FilterLeaf(BaseModel):
    fieldName: str
    stringFilter: StringFilter | None = None
    inListFilter: InListFilter | None = None
    numericFilter: NumericFilter | None = None
    betweenFilter: BetweenFilter | None = None


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
    dateRanges: list[DateRange] = Field(default=[], description="1 to 4 ranges.")
    dimensions: list[DimensionSpec] = Field(default=[], description="Up to 9.")
    metrics: list[MetricSpec] = Field(default=[], description="1 to 10.")
    dimensionFilter: FilterExpression | None = None
    metricFilter: FilterExpression | None = Field(
        default=None, description="Applied AFTER aggregation, on requested metrics."
    )
    limit: int | str | None = Field(
        default=None,
        json_schema_extra=_unverified(
            "int64 accepted as number or string; over 250000 silently capped; "
            "0 falls back to the 10000 default."
        ),
    )
    offset: int | str | None = None
    orderBys: list[OrderBy] = []
    metricAggregations: list[str] = Field(
        default=[], description="TOTAL, MAXIMUM and MINIMUM are supported."
    )
    keepEmptyRows: bool = False
    returnPropertyQuota: bool = False


class BatchRunReportsRequest(BaseModel):
    requests: list[RunReportRequest] = Field(default=[], description="1 to 5 requests.")


# ── Réponses ─────────────────────────────────────────────────────────────────


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
    metricValues: list[MetricValue] = []


class ResponseMetaData(BaseModel):
    currencyCode: str
    timeZone: str


class QuotaStatus(BaseModel):
    consumed: int | None = Field(default=None, description="Omitted when zero (proto3).")
    remaining: int | None = Field(default=None, description="Omitted when zero (proto3).")


class PropertyQuota(BaseModel):
    tokensPerDay: QuotaStatus = Field(
        json_schema_extra=_unverified("flat 10-token cost per report in this mock")
    )
    tokensPerHour: QuotaStatus
    concurrentRequests: QuotaStatus
    serverErrorsPerProjectPerHour: QuotaStatus
    potentiallyThresholdedRequestsPerHour: QuotaStatus


class RunReportResponse(BaseModel):
    dimensionHeaders: list[DimensionHeader] = []
    metricHeaders: list[MetricHeader] = []
    rows: list[Row] = Field(default=[], description="Key ABSENT when there are no rows.")
    totals: list[Row] = Field(
        default=[],
        json_schema_extra=_unverified(
            "RESERVED_TOTAL on every regular dimension; computed BEFORE metricFilter"
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
    customDefinition: bool = False


class MetricMetadata(BaseModel):
    apiName: str
    uiName: str
    description: str
    category: str
    type: str
    customDefinition: bool = False


class MetadataResponse(BaseModel):
    name: str
    dimensions: list[DimensionMetadata] = []
    metrics: list[MetricMetadata] = []


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


# Réponses d'erreur documentées sur chaque route de la surface Data.
REPONSES_ERREUR: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "INVALID_ARGUMENT"},
    401: {"model": ErrorResponse, "description": "UNAUTHENTICATED"},
    403: {"model": ErrorResponse, "description": "PERMISSION_DENIED"},
    429: {"model": ErrorResponse, "description": "RESOURCE_EXHAUSTED"},
}


def corps_requete(modele: type[BaseModel]) -> dict[str, Any]:
    """Bloc `requestBody` OpenAPI 3.1 auto-contenu ($defs locaux) : les
    handlers gardent leur `Request` brut — documenter n'est pas valider."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": modele.model_json_schema()}},
        }
    }

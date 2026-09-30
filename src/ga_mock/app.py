"""Application assembly.

The mock serves THREE surfaces on a single server:
  • `/token` — the oauth2.googleapis.com equivalent (service account flow);
  • `/v1beta/...` — the analyticsdata.googleapis.com equivalent;
  • out-of-contract: `/health`, `/__fixtures/*`, `/__admin/*` (never in the
    published OpenAPI).

In prod these are two distinct Google hosts — the consumer therefore
configures TWO URLs (`GA_API_URL`, `GA_TOKEN_URL`); here a single process is
enough, the paths don't overlap.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import (
    GRANT_TYPE_JWT_BEARER,
    TokenError,
    bearer_valid,
    fixture_service_account,
    token_response,
    validate_assertion,
)
from .errors import (
    MESSAGE_401_INVALID,
    MESSAGE_401_MISSING,
    MESSAGE_403_INVALID_PROPERTY,
    MESSAGE_403_PROPERTY,
    WWW_AUTHENTICATE_401_INVALID,
    WWW_AUTHENTICATE_401_MISSING,
    QuotaError,
    detail_missing_credentials,
    error,
    page_html_404,
)
from .injection import engine
from .models import (
    ERROR_RESPONSES,
    BatchRunReportsRequest,
    BatchRunReportsResponse,
    MetadataResponse,
    OAuthErrorResponse,
    RunReportRequest,
    RunReportResponse,
    TokenResponse,
    request_body,
)
from .registry import metadata_payload
from .report import RequestError, execute_run_report
from .settings import settings

app = FastAPI(title="Google Analytics 4 mock", version="0.1.0", docs_url="/docs")


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Unauthenticated — it is a probe, not an API surface."""
    return {"status": "ok", "service": "ga-mock"}


def _preprocess(request: Request) -> JSONResponse | None:
    """The SINGLE injection point, evaluated BEFORE authentication: an
    `auth_reject` must be able to preempt a valid bearer, a `latency` must
    apply even to a token call."""
    path = request.url.path
    engine.observe(path)
    return engine.evaluate(path)


def _report_summary(body: dict[str, object]) -> dict[str, object]:
    return {
        key: body[key]
        for key in ("dateRanges", "dimensions", "metrics", "limit", "offset")
        if key in body
    }


# What is NOT the vendor: these paths exist for the developer, and their
# errors must speak to them — not imitate a Google gateway.
AFFORDANCE_PATHS = ("/health", "/__admin", "/__fixtures", "/docs", "/openapi.json")

# Full gRPC method names, as the vendor writes them in the `details` of a
# "missing credential" 401.
RPC_METHOD = {
    "runReport": "google.analytics.data.v1beta.BetaAnalyticsData.RunReport",
    "batchRunReports": "google.analytics.data.v1beta.BetaAnalyticsData.BatchRunReports",
    "getMetadata": "google.analytics.data.v1beta.BetaAnalyticsData.GetMetadata",
}


def _check_bearer(request: Request, rpc_method: str) -> JSONResponse | None:
    """Guard for the DATA endpoints — google.rpc envelope, not RFC 6749.

    Only `/token` speaks the OAuth2 dialect; the rest of the surface answers
    like analyticsdata.googleapis.com.

    TWO distinct rejections, attested: header absent → "missing required
    authentication credential", with a google.rpc.ErrorInfo `details` and a
    `WWW-Authenticate` WITHOUT `error=`; token present but rejected → "had
    invalid authentication credentials", with no `details`, with
    `error="invalid_token"`. A client that doesn't know whether it should
    authenticate or RENEW reads exactly this difference.
    """
    authorization = request.headers.get("Authorization", "")
    if not authorization:
        return error(
            401,
            MESSAGE_401_MISSING,
            details=detail_missing_credentials(rpc_method),
            headers={"WWW-Authenticate": WWW_AUTHENTICATE_401_MISSING},
        )
    if authorization.startswith("Bearer ") and bearer_valid(authorization[7:]):
        return None
    return error(
        401, MESSAGE_401_INVALID, headers={"WWW-Authenticate": WWW_AUTHENTICATE_401_INVALID}
    )


@app.post(
    "/token",
    tags=["oauth2"],
    response_model=TokenResponse,
    responses={
        400: {
            "model": OAuthErrorResponse,
            "description": "unsupported_grant_type / invalid_grant / invalid_scope",
        }
    },
    summary="Service-account JWT-bearer token exchange",
)
async def token(request: Request) -> JSONResponse:
    """oauth2.googleapis.com/token endpoint — service account JWT-bearer flow.

    The body is parsed by hand (urllib) rather than via `request.form()`:
    Starlette delegates forms to python-multipart, and a urlencoded body
    doesn't justify widening the runtime dependencies.
    """
    if (failure := _preprocess(request)) is not None:
        return failure
    raw = (await request.body()).decode("utf-8", errors="replace")
    fields = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
    if fields.get("grant_type") != GRANT_TYPE_JWT_BEARER:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_grant_type",
                "error_description": f"Invalid grant_type: {fields.get('grant_type', '')}",
            },
        )
    try:
        claims = validate_assertion(fields.get("assertion", ""))
    except TokenError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": exc.code, "error_description": exc.description},
        )
    # 200 doesn't mean `access_token`: an unrecognized scope returns an
    # id_token ALONE, like the real endpoint.
    return JSONResponse(token_response(claims))


@app.get("/__fixtures/service-account.json", include_in_schema=False)
def fixture_sa(request: Request) -> JSONResponse:
    """Out of contract, like /__fixtures/remuneration.csv at
    boondmanager-mock: a dev convenience, not a Google path."""
    return JSONResponse(fixture_service_account(str(request.base_url).rstrip("/")))


def _check_property(property_id: str, *, allow_zero: bool = False) -> JSONResponse | None:
    """400 on a non-numeric identifier, 403 on ANOTHER property: the mock's
    token only has rights on the configured property — same behavior as a
    real, narrowly-scoped service account."""
    if not property_id.isdigit():
        return error(400, MESSAGE_403_INVALID_PROPERTY.format(id=property_id))
    allowed = {settings.property_id, "0"} if allow_zero else {settings.property_id}
    if property_id not in allowed:
        return error(403, MESSAGE_403_PROPERTY)
    return None


async def _json_body(request: Request) -> dict[str, object] | JSONResponse:
    """The body, in the vendor's JSON transcoder dialect — recorded case by case.

    Three behaviors nobody guesses:
      • an EMPTY body isn't a parsing error, it's an empty message — the
        request proceeds to validation and fails on
        `A dateRange is required.`;
      • a root that isn't an object (`null`, `[]`) has its own message,
        which talks about "Root element" rather than syntax;
      • broken syntax returns a MULTI-LINE message: the reason, then the
        faulty line, then a caret under the column. A consumer logging this
        message will see three lines in prod.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        return error(400, _invalid_json_message(raw, exc))
    if not isinstance(body, dict):
        return error(
            400,
            'Invalid JSON payload received. Unknown name "": Root element must be a message.',
        )
    return body


def _invalid_json_message(raw: str, exc: ValueError) -> str:
    """Reproduces the SHAPE of the transcoder's message: reason, excerpt, caret.

    The exact wording of the reason comes from protobuf's C++ parser
    ("Expected : between key:value pair.") and isn't reproducible from
    Python; this one uses its own parser's, and the approximation is
    recorded (`messages-erreurs-validation`). The geometry — three lines, a
    caret under the faulty column — is, though, faithful.
    """
    line, column = getattr(exc, "lineno", 1), getattr(exc, "colno", 1)
    lines = raw.splitlines() or [""]
    excerpt = lines[line - 1] if 0 < line <= len(lines) else ""
    reason = str(getattr(exc, "msg", exc)).split(":")[0]
    return f"Invalid JSON payload received. {reason}.\n{excerpt}\n{' ' * (column - 1)}^"


# The gRPC transcoding ":verb" pattern works as-is in Starlette: the
# `:runReport` literal follows the parameter in the SAME URL segment, and the
# compiled regex's backtracking correctly separates the two. Verified
# empirically before writing any logic on top of it.
@app.post(
    "/v1beta/properties/{property_id}:runReport",
    response_model=RunReportResponse,
    responses=ERROR_RESPONSES,
    openapi_extra=request_body(RunReportRequest),
    summary="Run a report",
)
async def run_report(request: Request, property_id: str) -> JSONResponse:
    if (failure := _preprocess(request)) is not None:
        return failure
    if (denial := _check_bearer(request, RPC_METHOD["runReport"])) is not None:
        return denial
    if (denial := _check_property(property_id)) is not None:
        return denial
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    engine.record_body(request.url.path, _report_summary(body))
    return _execute_guarded(body)


def _execute_guarded(body: dict[str, object]) -> JSONResponse:
    try:
        return JSONResponse(execute_run_report(body))
    except RequestError as exc:
        return error(400, str(exc), details=exc.details)
    except QuotaError as exc:
        return error(429, str(exc))


def _execute_batch(body: dict[str, object]) -> JSONResponse:
    requests = body.get("requests")
    if not isinstance(requests, list) or not requests:
        return error(400, "The batchRunReportsRequest must contain at least one runReportRequest.")
    if len(requests) > 5:
        return error(
            400,
            "Batch requests are limited to 5 requests.\n"
            f"  This batch request contains {len(requests)} requests.",
        )
    reports = []
    try:
        for sub_request in requests:
            if not isinstance(sub_request, dict):
                return error(400, "Invalid value for requests.")
            reports.append(execute_run_report(sub_request))
    except RequestError as exc:
        return error(400, str(exc), details=exc.details)
    except QuotaError as exc:
        return error(429, str(exc))
    return JSONResponse({"reports": reports, "kind": "analyticsData#batchRunReports"})


@app.post(
    "/v1beta/properties/{property_id}:batchRunReports",
    response_model=BatchRunReportsResponse,
    responses=ERROR_RESPONSES,
    openapi_extra=request_body(BatchRunReportsRequest),
    summary="Run up to 5 reports in one call",
)
async def batch_run_reports(request: Request, property_id: str) -> JSONResponse:
    """<=5 sub-reports; auth, property and JSON are checked ONCE at the batch
    level, then each sub-report goes through the full pipeline — one invalid
    sub-request fails the whole batch."""
    if (failure := _preprocess(request)) is not None:
        return failure
    if (denial := _check_bearer(request, RPC_METHOD["batchRunReports"])) is not None:
        return denial
    if (denial := _check_property(property_id)) is not None:
        return denial
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    requests = body.get("requests")
    if isinstance(requests, list):
        engine.record_body(
            request.url.path,
            {"requests": [_report_summary(d) for d in requests if isinstance(d, dict)]},
        )
    return _execute_batch(body)


@app.get(
    "/v1beta/properties/{property_id}/metadata",
    response_model=MetadataResponse,
    responses=ERROR_RESPONSES,
    summary="Dimensions and metrics available on the property",
)
def metadata(request: Request, property_id: str) -> JSONResponse:
    """The property's self-description — generated FROM the registry.

    `properties/0/metadata` is accepted, like at Google: zero designates the
    metadata common to all properties.
    """
    if (failure := _preprocess(request)) is not None:
        return failure
    if (denial := _check_bearer(request, RPC_METHOD["getMetadata"])) is not None:
        return denial
    if (denial := _check_property(property_id, allow_zero=True)) is not None:
        return denial
    return JSONResponse(metadata_payload(property_id))


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """ROUTING errors don't speak google.rpc — they don't even speak JSON.

    Attested on the real service: `POST …:runNothing`, `GET /v1beta/notOne`
    and `GET …:runReport` (wrong verb) ALL THREE render an HTML 404 page from
    the gateway, `text/html; charset=UTF-8`. There's no 405: the HTTP method
    is part of the route pattern, so a wrong verb is simply a route that
    doesn't exist.

    This is the only place on the surface where `response.json()` fails —
    and a consumer must learn it here, not in prod.

    The HTML page is reserved for VENDOR paths: on the mock's own
    affordances (`/health`, `/__admin`, `/__fixtures`), a typo must stay
    readable for a developer, not be disguised as a Google error.

    Other codes pass through unchanged: hiding a real bug behind a polished
    envelope would be worse than exposing it.
    """
    if exc.status_code in (404, 405):
        if any(request.url.path.startswith(prefix) for prefix in AFFORDANCE_PATHS):
            return JSONResponse(
                status_code=404,
                content={"error": f"unknown mock path {request.url.path}"},
            )
        return page_html_404(request.url.path)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Mounted ONLY if enabled: the control plane is ABSENT (not merely forbidden)
# when GA_MOCK_ADMIN_ENABLED doesn't ask for it — it appears neither in the
# routes nor in the published OpenAPI contract.
if settings.admin_enabled:
    from .admin import router as admin_router

    app.include_router(admin_router)


# ── Published contract ───────────────────────────────────────────────────────


def _references(obj: Any, refs: set[str]) -> None:
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            refs.add(ref.rsplit("/", 1)[1])
        for value in obj.values():
            _references(value, refs)
    elif isinstance(obj, list):
        for value in obj:
            _references(value, refs)


def _prune_orphan_schemas(schema: dict[str, Any]) -> None:
    """Transitive closure of $refs from the kept paths: removing a path
    removes its shapes too, including ones it alone referenced."""
    components = schema.get("components", {}).get("schemas", {})
    used: set[str] = set()
    _references(schema["paths"], used)
    while True:
        before = len(used)
        for name in list(used):
            if name in components:
                _references(components[name], used)
        if len(used) == before:
            break
    remaining = {name: components[name] for name in sorted(used) if name in components}
    if remaining:
        schema["components"]["schemas"] = remaining
    else:
        schema.pop("components", None)


def contract_openapi() -> dict[str, Any]:
    """The published contract: VENDOR paths only.

    /health, /__fixtures and /__admin are mock affordances — including them
    in the contract would misrepresent the Google surface. FastAPI's
    auto-documented 422 responses are removed for the same reason: the
    vendor answers 400 INVALID_ARGUMENT, never an HTTPValidationError.
    """
    schema = deepcopy(app.openapi())
    schema["info"] = {
        "title": "Google Analytics Data API v1beta — ga-mock contract",
        "version": app.version,
        "description": (
            "Surface reproduced by ga-mock: the OAuth2 service-account token "
            "exchange and the GA4 Data API v1beta core (runReport, "
            "batchRunReports, metadata). Sources of truth: the public GA4 Data "
            "API v1beta REST reference and its discovery document. Fields "
            "marked x-ga-confidence: unverified are registered in "
            "docs/UNVERIFIED-FIELDS.md."
        ),
    }
    schema["paths"] = {
        path: operations
        for path, operations in schema.get("paths", {}).items()
        if path == "/token" or path.startswith("/v1beta/")
    }
    for operations in schema["paths"].values():
        for operation in operations.values():
            if isinstance(operation, dict):
                operation.get("responses", {}).pop("422", None)
    _prune_orphan_schemas(schema)
    return schema

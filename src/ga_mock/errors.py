"""Google error envelope.

METHOD errors come out in serialized google.rpc.Status format:
`{"error": {"code", "message", "status"}}`. Never FastAPI's `{"detail": ...}`:
reproducing the real envelope is what lets the insights360 client write A
SINGLE error path, exercised in dev against this mock and unchanged in prod.

ROUTING errors, on the other hand, don't come out as JSON at all — observed
on analyticsdata.googleapis.com on 2026-09-02: an unknown path OR a wrong verb
both render an HTML 404 page from the Google gateway, `text/html`, no
envelope, and never 405. A consumer who calls `response.json()` on this case
breaks — in prod as here (cf. docs/CONFORMITE-REELLE.md).
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import HTMLResponse, JSONResponse

# HTTP → google.rpc.Code mapping, restricted to the codes the mock emits.
STATUSES: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}

# Two DISTINCT 401s, attested: `Authorization` header missing on one side,
# token present but rejected on the other. The realm, the `error=` suffix and
# the two messages are recorded verbatim — a client that distinguishes "not
# yet authenticated" from "expired token" reads exactly this.
WWW_AUTHENTICATE_401_MISSING = 'Bearer realm="https://accounts.google.com/"'
WWW_AUTHENTICATE_401_INVALID = 'Bearer realm="https://accounts.google.com/", error="invalid_token"'

MESSAGE_401_MISSING = (
    "Request is missing required authentication credential. Expected OAuth 2 "
    "access token, login cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)

MESSAGE_401_INVALID = (
    "Request had invalid authentication credentials. Expected OAuth 2 access "
    "token, login cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)


MESSAGE_403_INVALID_PROPERTY = (
    "Invalid property ID: {id}. A numeric Property ID is required. To learn "
    "more about Property ID, see "
    "https://developers.google.com/analytics/devguides/reporting/data/v1/property-id."
)


# The `details` that the JSON transcoding layer attaches to its 400s: one
# violation per faulty field, and `message` is their newline-joined
# concatenation. Recorded verbatim — `field` (PROTO path, snake_case) only
# comes with `description` when the layer can name the faulty field: an
# invalid enum value can, an unknown field name can't.
def detail_bad_request(violations: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "@type": "type.googleapis.com/google.rpc.BadRequest",
            "fieldViolations": [
                {"field": field, "description": description}
                if field
                else {"description": description}
                for field, description in violations
            ],
        }
    ]


def detail_missing_credentials(rpc_method: str) -> list[dict[str, Any]]:
    """The `details` the vendor attaches ONLY to the "missing credential" 401 —
    absent when the token is present but rejected. `rpc_method` is the full
    gRPC name of the targeted method."""
    return [
        {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "CREDENTIALS_MISSING",
            "domain": "googleapis.com",
            "metadata": {
                "method": rpc_method,
                "service": "analyticsdata.googleapis.com",
            },
        }
    ]


# The gateway page is reproduced in its STRUCTURE (status, MIME type, title)
# and not down to the markup: what matters to a consumer is that it isn't
# JSON, not Google's stylesheet.
_PAGE_404 = """<!DOCTYPE html>
<html lang=en>
  <meta charset=utf-8>
  <meta name=viewport content="initial-scale=1, minimum-scale=1, width=device-width">
  <title>Error 404 (Not Found)!!1</title>
  <p><b>404.</b> <ins>That's an error.</ins>
  <p>The requested URL <code>{path}</code> was not found on this server.
  <ins>That's all we know.</ins>
"""


def page_html_404(path: str) -> HTMLResponse:
    escaped = path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        status_code=404,
        content=_PAGE_404.format(path=escaped),
        headers={"Content-Type": "text/html; charset=UTF-8"},
    )


MESSAGE_403_PROPERTY = (
    "User does not have sufficient permissions for this property. To learn "
    "more about Property ID, see "
    "https://developers.google.com/analytics/devguides/reporting/data/v1/property-id."
)

MESSAGE_429_DAY = (
    "Exhausted property tokens for a property per day. "
    "These quota tokens will return in less than 24 hours."
)

# Each of the three token buckets has its own message. The wording follows the
# vendor-side bucket NAME (`tokensPerDay` and `tokensPerHour` are PROPERTY
# quotas, `tokensPerProjectPerHour` a PROJECT quota) — wording recorded as
# unattested: actually exhausting it would cost 24h of real quota.
MESSAGE_429_HOUR = (
    "Exhausted property tokens for a property per hour. "
    "These quota tokens will return in under an hour."
)

MESSAGE_429_PROJECT_HOUR = (
    "Exhausted property tokens for a project per hour. "
    "These quota tokens will return in under an hour."
)


class QuotaError(Exception):
    """Quota exhaustion → 429 RESOURCE_EXHAUSTED, Google-style wording."""


def error(
    code: int,
    message: str,
    *,
    status: str | None = None,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Builds the error response in the Google dialect.

    `details` stays empty in most cases: the real service puts
    google.rpc.BadRequest/QuotaFailure there, which few clients read. We only
    populate it when there's real content to put in it, never for decoration.
    """
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "status": status or STATUSES.get(code, "UNKNOWN"),
        }
    }
    if details:
        body["error"]["details"] = details
    response_headers = dict(headers or {})
    if code == 401:
        response_headers.setdefault("WWW-Authenticate", WWW_AUTHENTICATE_401_INVALID)
    return JSONResponse(status_code=code, content=body, headers=response_headers)

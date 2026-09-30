"""Replay the mock against a REAL GA4 property — the purgatory of UNVERIFIED.

`docs/UNVERIFIED-FIELDS.md` names this script in its `review_triggers`: it's
the only instrument that turns "plausible" into "attested". Each case is sent
TWICE — to the real service (analyticsdata.googleapis.com) and to the
in-process mock (`TestClient`) — then both responses are reduced to the same
SKELETON and compared.

What is compared, and what is not:
  • compared — the HTTP status, the PRESENCE of keys (the whole proto3 rule:
    empty repeated fields and zero int32s are absent), the JSON TYPE of leaf
    values (string vs number: this API's #1 trap), enum values
    (`TYPE_INTEGER`, `RESERVED_TOTAL`, `kind`, `status`) and the error
    wording;
  • NOT compared — the data. The real property isn't Boréal Conseil's world;
    requiring the same numbers wouldn't make sense.

Usage:

    GA_REAL_SA=/path/to/sa.json GA_REAL_PROPERTY=<property id> \\
        uv run python scripts/compare_real.py [--cases id …] [--output report.json]

The service account only needs `analytics.readonly` on the property, and the
Data API must be ENABLED on the account's project (otherwise 403
SERVICE_DISABLED, and the script says so plainly).

Crypto stays in stdlib, like the runtime: the real private key is read by a
minimal DER decoder (PKCS#8 → RSAPrivateKey) then signed by `ga_mock.rsa_min`.
No dependency enters the project for a verification script.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ga_mock.rsa_min import b64url, sign  # noqa: E402

DATA_HOST = "https://analyticsdata.googleapis.com"
TOKEN_HOST = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

# Identifier the service account has NO rights on: serves the "other property"
# case. Deliberately outside any plausible range.
FOREIGN_PROPERTY = "1"


# ── Reading the real private key (minimal DER, stdlib) ──────────────────────


def _der_read(data: bytes, position: int) -> tuple[int, bytes, int]:
    """Returns (tag, content, next position) for ONE DER element.

    Sufficient for PKCS#8: we only ever meet SEQUENCE, INTEGER and
    OCTET STRING, all in definite-length encoding. Not a general ASN.1
    parser.
    """
    tag = data[position]
    length = data[position + 1]
    position += 2
    if length & 0x80:
        octets = length & 0x7F
        length = int.from_bytes(data[position : position + octets], "big")
        position += octets
    return tag, data[position : position + length], position + length


def _der_integers(sequence: bytes, how_many: int) -> list[int]:
    values: list[int] = []
    position = 0
    while len(values) < how_many:
        tag, content, position = _der_read(sequence, position)
        if tag != 0x02:
            raise ValueError(f"expected INTEGER, got tag 0x{tag:02x}")
        values.append(int.from_bytes(content, "big"))
    return values


def private_key(pem: str) -> tuple[int, int, int]:
    """Unencrypted PKCS#8 PEM → (n, e, d).

    PrivateKeyInfo ::= SEQUENCE { version, algorithm, privateKey OCTET STRING }
    where the OCTET STRING holds RSAPrivateKey ::= SEQUENCE { version, n, e, d, … }.
    """
    body = "".join(line for line in pem.splitlines() if line and not line.startswith("-----"))
    data = base64.b64decode(body)
    _, info, _ = _der_read(data, 0)
    position = 0
    _, _, position = _der_read(info, position)  # version
    _, _, position = _der_read(info, position)  # AlgorithmIdentifier
    tag, envelope, _ = _der_read(info, position)
    if tag != 0x04:
        raise ValueError("expected OCTET STRING for privateKey")
    _, rsa, _ = _der_read(envelope, 0)
    _, n, e, d = _der_integers(rsa, 4)
    return n, e, d


# ── Real service side ────────────────────────────────────────────────────────


class Real:
    """HTTP client for the real service: signs an assertion, exchanges a
    bearer, calls. Nothing more — no retry, no cache: a replay must be a
    replay, not a simulation of a resilient client."""

    def __init__(self, sa_path: Path) -> None:
        self.sa = json.loads(sa_path.read_text())
        self.n, self.e, self.d = private_key(self.sa["private_key"])
        self._token = ""
        self._expiry = 0.0

    def _assertion(self) -> str:
        now = int(time.time())
        header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = b64url(
            json.dumps(
                {
                    "iss": self.sa["client_email"],
                    "scope": SCOPE,
                    "aud": self.sa.get("token_uri", TOKEN_HOST),
                    "iat": now,
                    "exp": now + 3600,
                }
            ).encode()
        )
        signature = b64url(sign(f"{header}.{payload}".encode(), self.n, self.d))
        return f"{header}.{payload}.{signature}"

    def bearer(self) -> str:
        if self._token and time.time() < self._expiry:
            return self._token
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(),
            }
        ).encode()
        status, _, text = _http(
            self.sa.get("token_uri", TOKEN_HOST),
            "POST",
            data,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            raise SystemExit(f"token exchange refused ({status}): {text}")
        payload = json.loads(text)
        self._token = str(payload["access_token"])
        self._expiry = time.time() + int(payload.get("expires_in", 3600)) - 120
        return self._token

    def call(self, case: Case, property_id: str) -> Response:
        headers = {"Content-Type": "application/json"}
        if case.auth == "valid":
            headers["Authorization"] = f"Bearer {self.bearer()}"
        elif case.auth == "invalid":
            headers["Authorization"] = "Bearer ya29.completely.fake"
        body = case.payload()
        status, response_headers, text = _http(
            DATA_HOST + case.path.replace("{p}", property_id), case.method, body, headers
        )
        return Response.from_raw(status, response_headers, text)


def _http(
    url: str, method: str, body: bytes | None, headers: dict[str, str]
) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


# ── Mock side ─────────────────────────────────────────────────────────────────


def mock_client() -> Any:
    from fastapi.testclient import TestClient

    import ga_mock

    ga_mock.state.reset()
    return TestClient(ga_mock.app), ga_mock


def call_mock(client: Any, module: Any, case: Case) -> Response:
    headers = {"Content-Type": "application/json"}
    if case.auth == "valid":
        token = client.post(
            "/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": module.build_assertion(),
            },
        ).json()["access_token"]
        headers["Authorization"] = f"Bearer {token}"
    elif case.auth == "invalid":
        headers["Authorization"] = "Bearer ya29.completely.fake"
    path = case.path.replace("{p}", module.settings.property_id)
    response = client.request(case.method, path, headers=headers, content=case.payload())
    return Response.from_raw(response.status_code, dict(response.headers), response.text)


# ── Skeleton: shape, stripped of data ────────────────────────────────────────


# A string is kept AS IS if it looks like an enum or a protocol marker;
# otherwise it becomes "str". That's what lets us compare `TYPE_INTEGER`,
# `RESERVED_TOTAL` or `analyticsData#runReport` without comparing a city name.
def _string(value: str) -> str:
    if value.startswith("analyticsData#"):
        return value
    enum = value.replace("_", "")
    if enum.isalnum() and any(c.isalpha() for c in enum) and value.upper() == value:
        return value
    return "str"


def _list(values: list[Any]) -> list[Any]:
    """Reduces a list to the UNION of its shapes: the row count is bound to
    differ between two worlds, the shape isn't."""
    shapes: list[Any] = []
    for element in values:
        shape = skeleton(element)
        if shape not in shapes:
            shapes.append(shape)
    return shapes


_SCALARS: tuple[tuple[type, str], ...] = ((bool, "bool"), (int, "int"), (float, "float"))


def skeleton(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: skeleton(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return _list(value)
    if isinstance(value, str):
        return _string(value)
    for python_type, name in _SCALARS:
        if isinstance(value, python_type):
            return name
    return "null"


class Response:
    __slots__ = ("body", "headers", "status", "text")

    def __init__(self, status: int, headers: dict[str, str], body: Any, text: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.text = text

    @classmethod
    def from_raw(cls, status: int, headers: dict[str, str], text: str) -> Response:
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return cls(status, {k.lower(): v for k, v in headers.items()}, body, text)

    @property
    def error_message(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            return str(self.body["error"].get("message", ""))
        return ""

    @property
    def error_status(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            return str(self.body["error"].get("status", ""))
        return ""


# ── Cases ─────────────────────────────────────────────────────────────────────


class Case:
    """One replay case. `path` carries `{p}`, replaced by each side's own
    property — the mock doesn't know the real id and vice versa."""

    def __init__(
        self,
        id: str,
        path: str,
        body: Any = None,
        *,
        method: str = "POST",
        auth: str = "valid",
        raw: str | None = None,
        note: str = "",
        subset: bool = False,
    ) -> None:
        self.id = id
        self.path = path
        self.body = body
        self.method = method
        self.auth = auth
        self.raw = raw
        self.note = note
        # The mock serves a SMALLER catalog than the real property: on
        # `metadata`, a shape present on the real side and absent from the
        # mock is an assumed scope reduction, not a divergence. The reverse —
        # a shape the mock invents — remains a gap.
        self.subset = subset

    def payload(self) -> bytes | None:
        if self.raw is not None:
            return self.raw.encode()
        if self.body is None:
            return None
        return json.dumps(self.body).encode()


REPORT = "/v1beta/properties/{p}:runReport"
BATCH = "/v1beta/properties/{p}:batchRunReports"
META = "/v1beta/properties/{p}/metadata"

# RELATIVE window everywhere: the mock's world is anchored in July 2026, the
# real property lives at real time — only relative dates yield rows on BOTH
# sides.
RANGE = [{"startDate": "30daysAgo", "endDate": "yesterday"}]
EMPTY = [{"startDate": "2015-01-01", "endDate": "2015-01-07"}]


def _base(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"dateRanges": list(RANGE), "metrics": [{"name": "sessions"}]}
    body.update(overrides)
    return body


CASES: list[Case] = [
    # ── Nominal shapes ───────────────────────────────────────────────────────
    Case("bare", REPORT, _base(), note="1 metric, 0 dimensions: row without dimensionValues"),
    Case(
        "dimension_date",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            metrics=[{"name": "sessions"}, {"name": "totalUsers"}],
        ),
    ),
    Case("empty", REPORT, _base(dateRanges=list(EMPTY)), note="omission of rows/rowCount"),
    Case(
        "float_metrics",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            metrics=[
                {"name": "engagementRate"},
                {"name": "bounceRate"},
                {"name": "averageSessionDuration"},
                {"name": "screenPageViewsPerSession"},
            ],
        ),
        note="float serialization and header types",
    ),
    Case(
        "metric_types",
        REPORT,
        _base(
            metrics=[
                {"name": "sessions"},
                {"name": "keyEvents"},
                {"name": "userEngagementDuration"},
                {"name": "sessionKeyEventRate"},
            ]
        ),
        note="expected TYPE_INTEGER / TYPE_FLOAT / TYPE_SECONDS",
    ),
    Case("limit_number", REPORT, _base(dimensions=[{"name": "date"}], limit=3)),
    Case("limit_string", REPORT, _base(dimensions=[{"name": "date"}], limit="3")),
    Case("limit_zero", REPORT, _base(dimensions=[{"name": "date"}], limit=0)),
    Case("limit_huge", REPORT, _base(dimensions=[{"name": "date"}], limit=300000)),
    Case("limit_negative", REPORT, _base(dimensions=[{"name": "date"}], limit=-1)),
    Case("offset", REPORT, _base(dimensions=[{"name": "date"}], limit=2, offset="3")),
    Case(
        "two_ranges",
        REPORT,
        _base(
            dateRanges=[
                {"startDate": "14daysAgo", "endDate": "8daysAgo"},
                {"startDate": "7daysAgo", "endDate": "yesterday"},
            ],
            dimensions=[{"name": "date"}],
        ),
        note="implicit dateRange dimension: presence AND position",
    ),
    Case(
        "named_ranges",
        REPORT,
        _base(
            dateRanges=[
                {"startDate": "14daysAgo", "endDate": "8daysAgo", "name": "before"},
                {"startDate": "7daysAgo", "endDate": "yesterday", "name": "after"},
            ],
            dimensions=[{"name": "date"}],
        ),
    ),
    Case(
        "aggregations",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            metricAggregations=["TOTAL", "MAXIMUM", "MINIMUM"],
        ),
        note="RESERVED_* markers and aggregation row shape",
    ),
    Case(
        "aggregation_count",
        REPORT,
        _base(dimensions=[{"name": "date"}], metricAggregations=["COUNT"]),
        note="COUNT is in the discovery enum — the mock rejects it",
    ),
    Case(
        "quota",
        REPORT,
        _base(returnPropertyQuota=True),
        note="EXACT list of PropertyQuota buckets",
    ),
    Case(
        "empty_rows",
        REPORT,
        _base(dimensions=[{"name": "date"}], dateRanges=list(EMPTY), keepEmptyRows=True),
    ),
    Case(
        "sort_metric",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            orderBys=[{"metric": {"metricName": "sessions"}, "desc": True}],
        ),
    ),
    Case(
        "sort_dimension",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            orderBys=[{"dimension": {"dimensionName": "date", "orderType": "ALPHANUMERIC"}}],
        ),
    ),
    Case(
        "string_filter",
        REPORT,
        _base(
            dimensions=[{"name": "deviceCategory"}],
            dimensionFilter={
                "filter": {
                    "fieldName": "deviceCategory",
                    "stringFilter": {"matchType": "EXACT", "value": "desktop"},
                }
            },
        ),
    ),
    Case(
        "regexp_filter",
        REPORT,
        _base(
            dimensions=[{"name": "pagePath"}],
            dimensionFilter={
                "filter": {
                    "fieldName": "pagePath",
                    "stringFilter": {"matchType": "PARTIAL_REGEXP", "value": "^/"},
                }
            },
        ),
    ),
    Case(
        "empty_filter",
        REPORT,
        _base(
            dimensions=[{"name": "sessionCampaignName"}],
            dimensionFilter={
                "notExpression": {"filter": {"fieldName": "sessionCampaignName", "emptyFilter": {}}}
            },
        ),
        note="emptyFilter exists in the discovery doc — absent from the mock",
    ),
    Case(
        "metric_filter",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            metricFilter={
                "filter": {
                    "fieldName": "sessions",
                    "numericFilter": {
                        "operation": "GREATER_THAN",
                        "value": {"int64Value": "1"},
                    },
                }
            },
        ),
    ),
    Case(
        "filter_on_unrequested_field",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            dimensionFilter={
                "filter": {
                    "fieldName": "country",
                    "stringFilter": {"value": "France"},
                }
            },
        ),
        note="filtering on a non-requested dimension: the mock rejects with 400",
    ),
    Case(
        "relative_dates",
        REPORT,
        _base(
            dateRanges=[{"startDate": "today", "endDate": "today"}],
            dimensions=[{"name": "date"}],
        ),
    ),
    Case(
        "metadata",
        META,
        None,
        method="GET",
        subset=True,
        note="the mock serves 20 dimensions and 15 metrics, not the whole catalog",
    ),
    Case(
        "metadata_zero",
        "/v1beta/properties/0/metadata",
        None,
        method="GET",
        subset=True,
        note="same — properties/0 describes the common schema",
    ),
    Case(
        "batch",
        BATCH,
        {"requests": [_base(), _base(dimensions=[{"name": "date"}], limit=2)]},
    ),
    Case("batch_six", BATCH, {"requests": [_base()] * 6}, note="the 5 sub-report cap"),
    Case("batch_empty", BATCH, {"requests": []}),
    # ── Request fields outside the mock's scope ─────────────────────────────
    Case(
        "currency",
        REPORT,
        _base(currencyCode="USD"),
        note="real RunReportRequest field, ignored by the mock",
    ),
    Case(
        "cohort",
        REPORT,
        {
            "cohortSpec": {
                "cohorts": [
                    {
                        "name": "c0",
                        "dimension": "firstSessionDate",
                        "dateRange": {"startDate": "14daysAgo", "endDate": "8daysAgo"},
                    }
                ],
                "cohortsRange": {"granularity": "DAILY", "endOffset": 5},
            },
            "dimensions": [{"name": "cohort"}, {"name": "cohortNthDay"}],
            "metrics": [{"name": "cohortActiveUsers"}],
        },
        note="the mock rejects cohortSpec with 400: assumed SCOPE divergence",
    ),
    # ── Errors ───────────────────────────────────────────────────────────────
    Case("no_bearer", REPORT, _base(), auth="none", note="401 + WWW-Authenticate"),
    Case("invalid_bearer", REPORT, _base(), auth="invalid"),
    Case(
        "other_property",
        f"/v1beta/properties/{FOREIGN_PROPERTY}:runReport",
        _base(),
        note="403 PERMISSION_DENIED",
    ),
    Case("non_numeric_property", "/v1beta/properties/abc:runReport", _base()),
    Case("unknown_dimension", REPORT, _base(dimensions=[{"name": "notADimension"}])),
    Case("unknown_metric", REPORT, _base(metrics=[{"name": "notAMetric"}])),
    Case("no_metric", REPORT, {"dateRanges": list(RANGE)}),
    Case("no_range", REPORT, {"metrics": [{"name": "sessions"}]}),
    Case(
        "reversed_range",
        REPORT,
        _base(dateRanges=[{"startDate": "yesterday", "endDate": "30daysAgo"}]),
    ),
    Case(
        "invalid_date",
        REPORT,
        _base(dateRanges=[{"startDate": "01/01/2026", "endDate": "today"}]),
    ),
    Case("five_ranges", REPORT, _base(dateRanges=list(RANGE) * 5)),
    Case(
        "ten_dimensions",
        REPORT,
        _base(
            dimensions=[
                {"name": n}
                for n in (
                    "date",
                    "week",
                    "month",
                    "yearMonth",
                    "dayOfWeek",
                    "country",
                    "region",
                    "city",
                    "browser",
                    "deviceCategory",
                )
            ]
        ),
    ),
    Case(
        "eleven_metrics",
        REPORT,
        _base(
            metrics=[
                {"name": n}
                for n in (
                    "sessions",
                    "totalUsers",
                    "activeUsers",
                    "newUsers",
                    "engagedSessions",
                    "engagementRate",
                    "bounceRate",
                    "averageSessionDuration",
                    "userEngagementDuration",
                    "screenPageViews",
                    "eventCount",
                )
            ]
        ),
    ),
    Case("duplicate_dimension", REPORT, _base(dimensions=[{"name": "date"}, {"name": "date"}])),
    Case("unknown_field", REPORT, _base(notAField=1), note="unknown JSON key"),
    Case("invalid_json", REPORT, None, raw="{this is not json"),
    Case("no_body", REPORT, None, raw=""),
    Case("unknown_route", "/v1beta/properties/{p}:runNothing", _base()),
    Case("wrong_verb", REPORT, None, method="GET"),
    Case(
        "unrequested_sort",
        REPORT,
        _base(
            dimensions=[{"name": "date"}],
            orderBys=[{"metric": {"metricName": "totalUsers"}}],
        ),
    ),
    Case(
        "invalid_aggregation",
        REPORT,
        _base(metricAggregations=["AVERAGE"]),
    ),
]


# ── Comparison ────────────────────────────────────────────────────────────────


def _diff_dict(expected: dict, actual: dict, path: str) -> list[str]:
    gaps: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        sub = f"{path}.{key}" if path else key
        if key not in actual:
            gaps.append(f"{sub}: absent from mock (real = {json.dumps(expected[key])})")
        elif key not in expected:
            gaps.append(f"{sub}: extra in mock ({json.dumps(actual[key])})")
        else:
            gaps.extend(_diff(expected[key], actual[key], sub))
    return gaps


def _diff_list(expected: list, actual: list, path: str) -> list[str]:
    if not expected and actual:
        return [f"{path}: real empty, mock non-empty"]
    if expected and not actual:
        return [f"{path}: real non-empty, mock empty"]
    missing = [f for f in expected if f not in actual]
    extra = [f for f in actual if f not in expected]
    return [f"{path}[]: shape absent from mock — {json.dumps(f)}" for f in missing] + [
        f"{path}[]: extra shape in mock — {json.dumps(f)}" for f in extra
    ]


def _diff(expected: Any, actual: Any, path: str = "") -> list[str]:
    """RECURSIVE diff of two skeletons. `expected` = the real side, `actual` =
    the mock: the naming says who's authoritative."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return _diff_dict(expected, actual, path)
    if isinstance(expected, list) and isinstance(actual, list):
        return _diff_list(expected, actual, path)
    if expected != actual:
        return [f"{path}: real={json.dumps(expected)} mock={json.dumps(actual)}"]
    return []


MISSING_FROM_MOCK = ": absent from mock"
MISSING_SHAPE = "[]: shape absent from mock"


def compare(case: Case, real: Response, mock: Response) -> dict[str, Any]:
    gaps: list[str] = []
    if real.status != mock.status:
        gaps.append(f"HTTP status: real={real.status} mock={mock.status}")
    if real.error_status != mock.error_status:
        gaps.append(f"error.status: real={real.error_status!r} mock={mock.error_status!r}")
    raw = _diff(skeleton(real.body), skeleton(mock.body))
    scope: list[str] = []
    for gap in raw:
        if case.subset and (MISSING_FROM_MOCK in gap or MISSING_SHAPE in gap):
            scope.append(gap)
        else:
            gaps.append(gap)
    if real.status == 401:
        real_header = real.headers.get("www-authenticate", "")
        mock_header = mock.headers.get("www-authenticate", "")
        if bool(real_header) != bool(mock_header):
            gaps.append("WWW-Authenticate: divergent presence")
    return {
        "case": case.id,
        "note": case.note,
        "real_status": real.status,
        "mock_status": mock.status,
        "real_message": real.error_message,
        "mock_message": mock.error_message,
        "real_www_authenticate": real.headers.get("www-authenticate", ""),
        "mock_www_authenticate": mock.headers.get("www-authenticate", ""),
        "gaps": gaps,
        "scope": scope,
        "real_skeleton": skeleton(real.body),
        "mock_skeleton": skeleton(mock.body),
    }


# ── Vocabulary: the VALUES that dimensions return ────────────────────────────

# Shape isn't enough. A mock rendering `mobile` where GA renders `mobile` but
# `Ile-de-France` where GA renders `Île-de-France` produces consumer code
# that breaks in prod on an `==`. These dimensions have a BOUNDED vocabulary:
# their real values are comparable as is, without comparing numbers.
VOCABULARY: tuple[str, ...] = (
    "date",
    "week",
    "month",
    "yearMonth",
    "dayOfWeek",
    "deviceCategory",
    "newVsReturning",
    "sessionDefaultChannelGroup",
    "sessionMedium",
    "sessionSource",
    "operatingSystem",
    "browser",
    "country",
    "region",
    "city",
)


def _values(response: Response) -> list[str]:
    if not isinstance(response.body, dict):
        return []
    return [row["dimensionValues"][0]["value"] for row in response.body.get("rows", [])]


def _value_shape(value: str) -> str:
    """Typographic signature of a value: it's what betrays a casing or accent
    gap, where the two worlds don't share cities anyway."""
    marks = []
    if value != value.lower():
        marks.append("Upper")
    if any(ord(c) > 127 for c in value):
        marks.append("accents")
    if value.startswith("(") and value.endswith(")"):
        marks.append("(marker)")
    if value.isdigit():
        marks.append(f"{len(value)}digits")
    return "+".join(marks) or "lowercase-ascii"


def compare_vocabulary(real: Real, client: Any, module: Any, property_id: str) -> list[dict]:
    results = []
    for dimension in VOCABULARY:
        case = Case(
            f"vocabulary:{dimension}",
            REPORT,
            _base(
                dateRanges=[{"startDate": "365daysAgo", "endDate": "yesterday"}],
                dimensions=[{"name": dimension}],
                limit=200,
            ),
        )
        real_values = _values(real.call(case, property_id))
        mock_values = _values(call_mock(client, module, case))
        real_shapes = {_value_shape(v) for v in real_values}
        mock_shapes = {_value_shape(v) for v in mock_values}
        results.append(
            {
                "dimension": dimension,
                "real": sorted(real_values)[:40],
                "mock": sorted(mock_values)[:40],
                "common": sorted(set(real_values) & set(mock_values))[:40],
                "real_shapes": sorted(real_shapes),
                "mock_shapes": sorted(mock_shapes),
                "divergent_shapes": sorted(real_shapes ^ mock_shapes),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="*", help="only run these ids")
    parser.add_argument("--output", type=Path, help="write the full report as JSON")
    parser.add_argument(
        "--details", action="store_true", help="print the skeletons of divergent cases"
    )
    parser.add_argument(
        "--vocabulary",
        action="store_true",
        help="compare the VALUES of bounded-vocabulary dimensions",
    )
    options = parser.parse_args()

    sa_path = os.environ.get("GA_REAL_SA")
    property_id = os.environ.get("GA_REAL_PROPERTY")
    if not sa_path or not property_id:
        print("GA_REAL_SA and GA_REAL_PROPERTY are required.", file=sys.stderr)
        return 2

    real = Real(Path(sa_path))
    client, module = mock_client()

    if options.vocabulary:
        vocabulary = compare_vocabulary(real, client, module, property_id)
        for entry in vocabulary:
            print(f"== {entry['dimension']}")
            print(f"   real: {', '.join(entry['real'][:20]) or '(no rows)'}")
            print(f"   mock: {', '.join(entry['mock'][:20]) or '(no rows)'}")
            print(f"   shapes real={entry['real_shapes']} mock={entry['mock_shapes']}")
            if entry["divergent_shapes"]:
                print(f"   ✗ divergent shapes: {entry['divergent_shapes']}")
        if options.output:
            options.output.write_text(json.dumps(vocabulary, indent=2, ensure_ascii=False))
            print(f"report → {options.output}")
        return 0

    selection = [c for c in CASES if not options.cases or c.id in options.cases]

    results = []
    for case in selection:
        real_response = real.call(case, property_id)
        if real_response.status == 403 and "SERVICE_DISABLED" in real_response.text:
            print(
                "The Data API is not enabled on the service account's project.\n"
                + real_response.error_message,
                file=sys.stderr,
            )
            return 3
        results.append(compare(case, real_response, call_mock(client, module, case)))

    divergent = [r for r in results if r["gaps"]]
    width = max(len(r["case"]) for r in results)
    for result in results:
        mark = "✗" if result["gaps"] else "✓"
        print(
            f"{mark} {result['case']:<{width}}  "
            f"{result['real_status']}/{result['mock_status']}"
            + (f"  {len(result['gaps'])} gap(s)" if result["gaps"] else "")
        )
        for gap in result["gaps"]:
            print(f"    · {gap}")
        for out_of_scope in result.get("scope", []):
            print(f"    ~ assumed out of scope: {out_of_scope}")
        if options.details and result["gaps"]:
            print(f"    real: {json.dumps(result['real_skeleton'], ensure_ascii=False)}")
            print(f"    mock: {json.dumps(result['mock_skeleton'], ensure_ascii=False)}")

    print(f"\n{len(results) - len(divergent)}/{len(results)} cases conforming")
    if options.output:
        options.output.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"report → {options.output}")
    return 1 if divergent else 0


if __name__ == "__main__":
    raise SystemExit(main())

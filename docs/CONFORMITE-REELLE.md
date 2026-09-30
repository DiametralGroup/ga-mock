---
type: attestation
description: >
  Observations made AGAINST the real Google service and mock conformance
  verdicts. Each line is a dated, reproducible observation
  (`scripts/compare_real.py`); this document is the source that authorizes
  removing an entry from UNVERIFIED-FIELDS.md.
sources:
  - analyticsdata.googleapis.com — real GA4 property, service account
  - oauth2.googleapis.com/token — JWT-bearer flow, real private key
  - https://analyticsdata.googleapis.com/$discovery/rest?version=v1beta (rev. 20260831)
tool: scripts/compare_real.py
date_recorded: 2026-09-02
result: 53/53 conforming cases; 21 error messages identical character for character
---

# Conformance recorded against the real service

The mock was confronted with the real Google service on **2026-09-02**: 53
shape cases (`make compare`) and a vocabulary comparison (`make compare
ARGS=--vocabulaire`), plus about thirty probes targeted at `/token` and the
gateway. Three sources, three different authorities: the **discovery
document** is authoritative on SHAPES (it is generated from the vendor's
protos), **HTTP recordings** are authoritative on behaviors and wordings, and
whatever could not be observed stays in
[UNVERIFIED-FIELDS.md](UNVERIFIED-FIELDS.md).

What follows is the minutes. "Fixed" lines produced code; "conforming" lines
produced a test that pins what was already correct.

## 1. The discovery document (rev. 20260831)

| Finding | Mock before | Verdict |
|---|---|---|
| `PropertyQuota` carries **six** buckets: `tokensPerProjectPerHour` was missing | 5 buckets | **fixed** — bucket added, decremented and capped at 14,000 (35% of the hourly one, a rule written in the discovery doc, value confirmed on the real property) |
| `Filter.emptyFilter` exists | rejected with 400 | **fixed** — predicate implemented |
| `RunReportRequest` carries `currencyCode` | ignored | **fixed** — the value is returned in `metadata.currencyCode` |
| `RunReportRequest` carries `property`, `cohortSpec`, `comparisons` | `cohortSpec`/`comparisons` rejected | assumed SCOPE gap, §4 |
| `ResponseMetaData` carries 5 more fields | absent | **conforming** — the service does not render them either on a nominal response (proto3 omission) |

## 2. The `analyticsdata.googleapis.com` gateway

These cases require NEITHER API activation NOR rights on a property.

| Case | Real | Mock before | Verdict |
|---|---|---|---|
| `POST :runReport` with no `Authorization` header | `401`, *"Request is **missing** required authentication credential…"*, `details[0].reason = CREDENTIALS_MISSING` with the method's gRPC name, `WWW-Authenticate: Bearer realm="https://accounts.google.com/"` | "had invalid" message, no `details`, realm with `error="invalid_token"` | **fixed** — two distinct 401s |
| `POST :runReport` with a fake bearer | `401`, *"Request **had invalid** authentication credentials…"*, **no** `details`, `WWW-Authenticate: …, error="invalid_token"` | identical | **conforming** |
| `POST …:runNothing` (unknown verb) | `404`, `text/html; charset=UTF-8` | JSON `NOT_FOUND` envelope | **fixed** — HTML page |
| `GET /v1beta/notAResource` | `404` HTML | JSON envelope | **fixed** |
| `GET …:runReport` (wrong verb) | **`404`** HTML — never 405 | `405 METHOD_NOT_ALLOWED` | **fixed** — `METHOD_NOT_ALLOWED` removed from the status table |
| broken JSON body with no bearer | `401` — auth precedes parsing | identical | **conforming** |

The HTML page is reproduced in its STRUCTURE (status, `Content-Type`, title
`Error 404 (Not Found)!!1`), not down to the markup: what matters is that
`response.json()` fails. It is reserved for vendor paths — a typo on
`/__admin` keeps a readable error.

## 3. The `runReport` / `batchRunReports` / `metadata` surface

### Fixed

| Recorded finding | Mock before |
|---|---|
| **A filter does NOT have to target a requested field**: filtering on `country` while grouping by `date` works, and a `metricFilter` can target a metric absent from the report | 400 "must be a requested dimension" — a 400 in dev where prod answers 200 |
| **A report WITHOUT a metric is valid**: bare dimension rows, no `metricHeaders`, no `metricValues`. Only the absence of BOTH is rejected | 400 "at least one metric" |
| **Unknown JSON keys are rejected** by the transcoding layer, BEFORE the method runs: `Invalid JSON payload received. Unknown name "x": Cannot find field.`, one `fieldViolation` per key, message = their concatenation. Nested keys name the PROTO path: `at 'date_ranges[0]'` | silently ignored |
| **Constant date bounds**: `start_date` must be STRICTLY after `2015-08-13` (the 13th itself is rejected) and before `3000-01-01` | no bound |
| **Double serialization = protobuf's `DoubleToBuffer`**: `.15g`, and only `.17g` if it does not round-trip — never 16. Python's `repr` gives `0.9734848484848485` where the service gives `0.97348484848484851`. Rule verified on 21 values | `repr()` |
| **`QuotaStatus` always renders both fields**, `consumed: 0` included — an exception to the proto3 omission rule | zeros omitted |
| **Token cost is not flat**: 1 for a short, narrow report, 2 for 1 dim x 10 metrics, 4 for 9 x 10, 7 for 1 x 1 over 365 days | flat rate of 10 |
| `metadata`: **`customDefinition` is ABSENT** when it is false | rendered as `false` |
| `metadata`: `deprecatedApiNames` on `dayOfWeek` (`dayOfWeekZero`), `sessionDefaultChannelGroup` (`sessionDefaultChannelGrouping`), `keyEvents` (`conversions`), `sessionKeyEventRate` (`sessionConversionRate`) | absent |
| `metadata`: category `Traffic Source` (capital S), `newVsReturning` -> `User`, `userEngagementDuration` -> `User`, `sessionKeyEventRate` -> `Session` | `Traffic source`, `User lifetime`, `Session`, `Event` |
| `metadata`: `sessionsPerUser` is named **"Sessions per active user"** — the denominator is `activeUsers`, not `totalUsers` | wrong name and formula |
| `metadata`: nine standard `comparisons` on a property, NONE on `properties/0` | absent |
| **An empty body is not a parsing error**: it goes through as an empty message and fails on `A dateRange is required.` | "Invalid JSON payload received." |
| Non-object root (`null`, `[]`) -> `Invalid JSON payload received. Unknown name "": Root element must be a message.` | same generic message |
| Broken JSON -> **multiline** message: reason, offending line, caret under the column | a single line |
| **The ORDER of checks**: transcoding -> field name validity -> `limit`/`offset` -> date range -> cardinality bounds -> "dimensions and/or metrics". Ten dimensions WITHOUT a range fail on `A dateRange is required.` | bounds before the range |
| **All validation wordings**: `A dateRange is required.`, `start_date must be less than or equal to end_date. start_date = … and end_date = …`, `Invalid startDate : …. startDate must be YYYY-MM-DD, NdaysAgo, yesterday, or today.`, `Requests are limited to 4 dateRanges.\n  This request contains N dateRanges.`, `… 9 dimensions within a nested request.`, `… 10 metrics within a nested request.`, `Found duplicate dimensions: X`, `Duplicate metrics are not allowed. Found duplicate metrics: X`, `limit must be positive. The API received limit = -1`, `offset must be positive. …`, `Field X exists in OrderBy but is not defined in input Dimensions/Metrics list`, `Metric aggregation Count is not supported in ReportRequest.`, `Invalid value at 'metric_aggregations[0]' (…MetricAggregation), "X"`, `Batch requests are limited to 5 requests.\n  This batch request contains N requests.`, `The batchRunReportsRequest must contain at least one runReportRequest.`, `Invalid property ID: abc. A numeric Property ID is required. …`, `Field X is not a valid dimension. For a list …` (ONE space) and `… is not a valid metric.  For a list …` (TWO spaces) | invented wordings |

### Conforming — pinned by a test

- Aggregation markers `RESERVED_TOTAL` / `RESERVED_MAX` / `RESERVED_MIN`
  (not `RESERVED_MAXIMUM`, despite what the docs suggest).
- Implicit `dateRange` dimension appended in LAST position, unnamed ranges
  labeled `date_range_0`, `date_range_1`…
- Default order with no `orderBys`: first metric DESCENDING.
- `limit: 0` falls back to the default; beyond 250,000, silently capped;
  negative `limit` rejected.
- int64 (`limit`, `offset`) accepted as a JSON number OR a string.
- Metric values always as strings; integer floats rendered without `.0`;
  `rows`/`rowCount` absent when empty; `kind` always present.
- `keyEvents` is indeed **TYPE_FLOAT** (that was a hypothesis).
- Geographic names WITHOUT accents (`Auvergne-Rhone-Alpes`,
  `Asnieres-sur-Seine`, `Baden-Wurttemberg`) — the one exception found is
  `country`, where `Côte d'Ivoire` keeps its accent.
- Formats: `date` as `YYYYMMDD`, `week`/`month` on two digits, `yearMonth` on
  six, `dayOfWeek` from 0 to 6, `deviceCategory` lowercase,
  `sessionDefaultChannelGroup` capitalized.
- The mock's 20 dimensions and 15 metrics ALL exist on the real property, with
  the same types.

## 4. `oauth2.googleapis.com/token`

Attested order: structure -> signature -> `iss` -> `iat`/`exp` presence ->
window -> empty `scope` -> `aud`.

| Case | Real | Verdict |
|---|---|---|
| unknown / missing `grant_type` | `400 unsupported_grant_type` / `Invalid grant_type: <value>` | **conforming** |
| missing, empty, != 3 segments, invalid base64, non-JSON or non-object segment assertion | `400` **`invalid_request`** / **`Bad Request`** — no explanation | **fixed** |
| wrong signature | `400 invalid_grant` / `Invalid JWT Signature.` | **conforming** |
| `alg` = HS256, `none`, or absent | `400 invalid_grant` / **`Invalid JWT Signature.`** — no dedicated message | **fixed** |
| unknown or missing `iss` | `400 invalid_grant` / `Invalid grant: account not found` | **conforming** |
| missing `iat` / `exp` | dedicated messages: `Invalid JWT: iat (issued at) is not set.` / `… exp (expiration time) …` | **fixed** |
| expired / duration > 60 min / future `iat` | the long "short-lived token (60 minutes)" message | **conforming**, character for character |
| `iat`/`exp` as a **digit string** | `200` — accepted | **fixed** |
| empty `scope` | `400 invalid_scope` / `Invalid OAuth scope or ID token audience provided.` | **conforming** |
| well-formed but unrecognized `scope`, or MIXED (one valid + one unknown) | **`200 {"id_token": …}`** — no `access_token` | **fixed** |
| missing `aud`, other host, or without `/token` | `400 invalid_grant` / **`Invalid JWT: Failed audience check.`** | **fixed** (wording), tolerance kept — §5 |
| nominal | `200`, `access_token`, **`expires_in: 3599`**, `token_type: Bearer` | **conforming** — the 3599 was a hypothesis |

Real `id_token` claims, reproduced: `iss=https://accounts.google.com`,
`aud=<the requested scope>`, `azp`/`email` = account address,
`email_verified`, `sub` = numeric id, `iat`, `exp`; header `{alg, kid, typ}`.

## 5. ASSUMED gaps

The real behavior is known; the gap is a choice.

- **Catalog.** The mock serves 20 dimensions and 15 metrics; the real
  property advertises several hundred (including `TYPE_CURRENCY` and
  `TYPE_MILLISECONDS` ones). The harness counts this as "out of scope", not as
  a gap: what the mock declares is accurate, it just declares less of it.
- **Tolerant `aud`.** The vendor requires strict equality; the mock accepts
  any audience ending in `/token` — behind compose, the assertion targets
  `http://ga-mock:8000/token`. The rejection borrows the real message.
- **"Recognized" scopes.** The switch to `id_token` is attested, but the mock
  only knows the two Analytics scopes: a valid but foreign Google scope gets
  an `id_token` here and an `access_token` at Google.
- **`cohortSpec` / `comparisons` rejected with 400.** The vendor accepts them.
  Silently ignoring them would suggest they had taken effect.
- **Out-of-scope endpoints.** `runPivotReport`, `batchRunPivotReports`,
  `runRealtimeReport`, `checkCompatibility`, `audienceExports` render a
  routing 404.
- **Bearer prefix.** `ya29.mock.` instead of `ya29.c.`: a mock token must stay
  recognizable in a log.
- **"Did you mean … ?" suggestions** not reproduced — they are computed
  against Google's full catalog.

## 6. Open decision: the generated world's vocabulary

The one substantive gap that has NOT been fixed, because it touches the world
and not the dialect: **the mock never produces `(not set)`**, where the
service renders it on almost every dimension. It also skips `smart tv`,
`Cross-network`, `AI Assistant`, `(data not available)`, `referral_profile`…

A consumer that writes `if device == "mobile" … elif device == "tablet"` with
no default branch will pass against the mock and break in prod.

Fixing it requires modifying `dataset/sessions.py`, hence changing the world
at constant seed — which invalidates every fixture pinned by consumers
(insights360 foremost). This is a publication trade-off, not a technical fix:
it awaits a decision.

## 7. Replay

```bash
GA_REAL_SA=/path/to/sa.json GA_REAL_PROPERTY=<id> make compare
GA_REAL_SA=… GA_REAL_PROPERTY=… make compare ARGS=--vocabulaire
```

The service account needs `analytics.readonly` on the property, and the
**Google Analytics Data** and **Admin** APIs must be enabled on its
project — otherwise `403 SERVICE_DISABLED`, and the script exits with code 3
saying so.

# ga-mock

[![CI](https://github.com/LittleBigCode/ga-mock/actions/workflows/ci.yml/badge.svg)](https://github.com/LittleBigCode/ga-mock/actions/workflows/ci.yml)

A Google Analytics 4 **Data API v1beta** mock, sibling of
[boondmanager-mock](https://github.com/LittleBigCode/boondmanager-mock): the
same fictional world (Boréal Conseil, a French IT consultancy of 34 people),
seen through its corporate-website analytics. Consumers point `GA_API_URL` and
`GA_TOKEN_URL` at this server in dev and at the real Google endpoints
(`analyticsdata.googleapis.com`, `oauth2.googleapis.com`) in prod — the client
code path is identical.

Session-level by design: the dataset is ~32,000 individual sessions
(2025-01-01 → 2026-07-15), so `totalUsers` is an **exact** distinct count over
any date range — not an additive approximation that would lie precisely where
GA4 is tricky.

## Start in one command

```bash
docker run -p 8013:8000 -e GA_MOCK_ADMIN_ENABLED=true ghcr.io/littlebigcode/ga-mock:latest
```

or from a checkout: `docker compose up --build` (same, via `make up`), or
without Docker: `make bootstrap && make run`.

## Credentials and a full token flow, ready to paste

Auth is **really validated**: RS256 signature, issuer, time window and scope
are checked against the committed, overtly-fake service-account keypair. A
badly signed assertion fails HERE, not in prod.

The standard service-account JSON (with the fake PEM, `token_uri` rewritten to
this server) is served out-of-contract:

```bash
curl -s http://localhost:8013/__fixtures/service-account.json
```

Exchange a signed assertion for a bearer (this literal assertion is valid
against the default configuration — the virtual clock is anchored, so it does
not expire until you advance the clock):

```bash
ASSERTION='eyJhbGciOiAiUlMyNTYiLCAidHlwIjogIkpXVCJ9.eyJpc3MiOiAiaW5zaWdodHMzNjBAYm9yZWFsLWNvbnNlaWwtbW9jay5pYW0uZ3NlcnZpY2VhY2NvdW50LmV4YW1wbGUiLCAic2NvcGUiOiAiaHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC9hbmFseXRpY3MucmVhZG9ubHkiLCAiYXVkIjogImh0dHA6Ly9sb2NhbGhvc3Q6ODAxMy90b2tlbiIsICJpYXQiOiAxNzg0MTE4NjAwLCAiZXhwIjogMTc4NDEyMjIwMH0.h0EqNKmZCXyMPkTBj561viFR81FprCmWhkMnTgpDN96Q_hPx7-R6anqtaVtKXRYvpp2reaMB29DgVDs8Rb486G73xQ1AdoUMtDwPrTTdsu4p-SH5QOWQSyCdr3xGWqal1n5PMslp6hwBorC60wM5uLgPIwlhkI4DvcpvnPtdjH8t3H-DNJxPRpxmEFBn_cpBzV1aVq5sQ_eFfJxEOZ43eindbdaSDTJt0UdChn3HjmYlFUCPba1MebHV05UXxZwIl0rEgQvVv295Ptd85d7rr5AfFGX8xIZuyMxtzRGkm_kQ_h_OSXEjBcfECXLK40O_uyUIWHLK2hNGuIRznANOgA'
TOKEN=$(curl -s -X POST http://localhost:8013/token \
  -d "grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer" \
  -d "assertion=$ASSERTION" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
```

Regenerate an assertion at will (also the way consumer test suites mint
tokens, with zero crypto dependency):

```bash
uv run python -c "from ga_mock import build_assertion; print(build_assertion())"
```

Run a report:

```bash
curl -s -X POST "http://localhost:8013/v1beta/properties/424242001:runReport" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dateRanges":[{"startDate":"7daysAgo","endDate":"today"}],
       "dimensions":[{"name":"date"}],
       "metrics":[{"name":"sessions"},{"name":"totalUsers"}]}'
```

Control plane (when `GA_MOCK_ADMIN_ENABLED=true`):

```bash
curl -s -H "X-Mock-Admin-Token: mock-admin-token" http://localhost:8013/__admin/state
```

Gotchas that are faithful on purpose — every one of these was **recorded on
the real Google endpoints** (see
[docs/CONFORMITE-REELLE.md](docs/CONFORMITE-REELLE.md)):

- no `Authorization` header → `401` *"Request is **missing** required
  authentication credential"*, with a `details[].reason = CREDENTIALS_MISSING`
  and `WWW-Authenticate: Bearer realm="…"`; a **rejected** bearer → `401`
  *"Request **had invalid** authentication credentials"*, no `details`, and
  `error="invalid_token"` on the header. Two different signals, deliberately.
- an unknown path, an unknown verb **and a wrong HTTP method** all return a
  `404` **HTML** page from the gateway — never a JSON envelope, never a `405`.
  `response.json()` raises there, in dev as in prod.
- a `/token` assertion that cannot be decoded → `400 invalid_request` /
  `"Bad Request"`, with no explanation at all.
- a `/token` `scope` that is well-formed but unrecognized → **`200` with an
  `id_token` and no `access_token`**. A scope typo does not fail loudly.
- an **unknown JSON key** in a report body → `400`, one `fieldViolation` per
  key. `dateRange` instead of `dateRanges` breaks here, not silently.
- an **empty body** is not a parse error — it is an empty message, and fails on
  `A dateRange is required.`
- `startDate` must be strictly after **2015-08-13**; a report with **no
  metrics** is valid; a filter may target a field the report does not group by.
- another property id → `403 PERMISSION_DENIED`.

## Two modes, both maintained

- **in-process** — `TestClient(ga_mock.app)` inside a test suite;
- **container** — the published image for compose stacks and CI sidecars.

The application the stack queries IS the one the tests exercise.

## Served surface

| Path | Purpose |
|---|---|
| `POST /token` | oauth2.googleapis.com service-account JWT-bearer exchange |
| `POST /v1beta/properties/{id}:runReport` | the workhorse report endpoint |
| `POST /v1beta/properties/{id}:batchRunReports` | up to 5 reports per call |
| `GET /v1beta/properties/{id}/metadata` | dimensions/metrics — generated FROM the registry (`properties/0` accepted) |
| `GET /health` | unauthenticated probe |
| `GET /__fixtures/service-account.json` | fake SA JSON, `token_uri` rewritten to this server (out of contract) |
| `POST /__admin/*` | reset / state / inject / clock (mounted only when enabled) |

20 dimensions, 15 metrics — the exact list is what `GET …/metadata` returns,
and the OpenAPI contract lives in `contracts/ga4-data.openapi.yaml`
(regenerated by `make contract`, equality enforced by a test).

## The reproduced dialect

| Aspect | Behavior |
|---|---|
| int64 fields (`limit`, `offset`) | accepted as JSON number OR string (proto3 JSON) |
| metric/dimension values | always serialized as **strings** — doubles follow protobuf's `DoubleToBuffer` (`.15g`, else `.17g`, never 16) |
| empty repeated fields | key **absent** — never `"rows": []`, and no `"rowCount"` when zero |
| `date` values | `YYYYMMDD`; relative dates `today`/`yesterday`/`NdaysAgo` resolve against the **virtual clock** |
| 2–4 `dateRanges` | implicit `dateRange` dimension appended to headers |
| `limit` | default 10000, values over 250000 silently capped |
| filters | full `FilterExpression` trees; filter fields must be requested in the report |
| `metricAggregations` | `TOTAL` (exact global dedup) / `MAXIMUM` / `MINIMUM` with `RESERVED_*` markers |
| `returnPropertyQuota` | the **six** standard buckets — `tokensPerProjectPerHour` (35% of the hourly one) runs out first; `consumed` is always present, `0` included; a report costs 1 to 7 tokens depending on span and cell count |
| filter leaves | `stringFilter`, `inListFilter`, `numericFilter`, `betweenFilter`, `emptyFilter` |
| method errors | `{"error": {code, message, status}}` with matching HTTP code; `/token` speaks RFC 6749 instead |
| routing errors | an HTML `404` from the gateway — no envelope, and no `405` anywhere |

The mock was replayed against a **real GA4 property on 2026-09-02**: 53 shape
cases conform, and 21 error messages match the vendor character for character.
The full minutes — what was corrected, what was already right, and what is
deliberately different — are in
[docs/CONFORMITE-REELLE.md](docs/CONFORMITE-REELLE.md); what could not be
observed stays quarantined in
[docs/UNVERIFIED-FIELDS.md](docs/UNVERIFIED-FIELDS.md), where a test fails if
an approximation is not registered. `make compare` runs the replay again.

One gap is knowingly open: the generated world never emits `(not set)`, which
the real API returns on nearly every dimension. Closing it means changing the
world at constant seed — see CONFORMITE-REELLE §6.

## The dataset

Deterministic, seeded (`GA_MOCK_SEED`, default 42): same seed, same world,
byte-for-byte. Each day is a pure function of `(seed, day)` — history can
never be rewritten. The world tells the life of Boréal Conseil: ~40–120
sessions/day with weekday/holiday/summer seasonality, a ~32-page site
(`/expertises/*` mirroring the four business units, `/realisations/*`,
`/blog/*`, `/offres-emploi/*` matching the CRM world's job postings), seven
named campaigns (`recrutement-cyber-2026`, `livre-blanc-data-mesh`, …) that
convert more, monthly `newsletter-YYYY-MM` email campaigns, and stable
returning-visitor ids so user dedup is real. Key events: `generate_lead`
(contact form) and `job_apply` (postings).

## Freshness and the virtual clock

GA4 keeps back-filling recent days for ~48 h; so does the mock
(`GA_MOCK_FRESHNESS_HOURS`). Recent days serve a growing, monotonic subset of
their sessions; days out of the window are complete and immutable. The clock
is **anchored** at 2026-07-15T14:30+02:00 (same world date as
boondmanager-mock) and only moves via:

```bash
curl -s -X POST -H "X-Mock-Admin-Token: mock-admin-token" \
  -d '{"advance_seconds": 86400}' http://localhost:8013/__admin/clock
```

Advancing the clock ages relative dates, freshness AND bearer expiry together
(a client must renew its token after a big jump — deliberately). This is the
lever that lets a consumer prove its "re-extract the last N days" incremental
pattern; see [docs/features/freshness.md](docs/features/freshness.md).

## Failure modes

`POST /__admin/inject` with `kind`:

| kind | Reproduces |
|---|---|
| `rate_limit` | 429 RESOURCE_EXHAUSTED after N requests, with `Retry-After` |
| `quota_exhausted` | 429 daily-quota exhaustion |
| `status` | any 5xx (transient with `times`, persistent without) |
| `latency` | slow upstream (`seconds`) |
| `auth_reject` | 401 that preempts a VALID bearer |

Scopes are globs (`*:runReport`, `/v1beta/*`, `/token`, `*`);
`GA_MOCK_RATE_LIMIT_AFTER` sets a baseline rule that survives resets.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GA_MOCK_HOST` / `GA_MOCK_PORT` | `0.0.0.0` / `8000` | bind address/port |
| `GA_MOCK_SEED` | `42` | dataset seed |
| `GA_MOCK_PROPERTY_ID` | `424242001` | the only property served (others → 403) |
| `GA_MOCK_SA_EMAIL` | `insights360@boreal-conseil-mock.iam.gserviceaccount.example` | expected JWT `iss` |
| `GA_MOCK_FRESHNESS_HOURS` | `48` | processing-latency window |
| `GA_MOCK_ADMIN_ENABLED` | `false` | mounts `/__admin` (absent otherwise) |
| `GA_MOCK_ADMIN_TOKEN` | `mock-admin-token` | `X-Mock-Admin-Token` value |
| `GA_MOCK_QUOTA_TOKENS_PER_DAY` / `_PER_HOUR` | `200000` / `40000` | property quota buckets |
| `GA_MOCK_QUOTA_TOKENS_PER_PROJECT_PER_HOUR` | `14000` | project bucket — 35% of the hourly one, per Google |
| `GA_MOCK_RATE_LIMIT_AFTER` / `GA_MOCK_RETRY_AFTER` | — / `1` | baseline rate-limit injection |

## Development

```bash
make bootstrap   # uv sync
make test        # pytest
make lint        # ruff check + format --check + mypy strict
make format      # ruff format + autofix
make run         # local server with the control plane enabled
make image       # docker build
make contract    # regenerate contracts/ga4-data.openapi.yaml — REVIEW the diff
make compare     # replay against a REAL GA4 property (GA_REAL_SA, GA_REAL_PROPERTY)
```

`make compare` is how an approximation stops being one: it sends the same ~45
cases to `analyticsdata.googleapis.com` and to the in-process mock, then diffs
the response *shapes* (key presence, JSON types, enum values, error wordings) —
never the numbers, since the real property is not Boréal Conseil.
`make compare ARGS=--vocabulaire` compares the VALUES of bounded dimensions
instead, which is what settles questions like accents in `region`/`city`.

The fake RSA keypair is committed on purpose (it authenticates a mock, i.e.
nothing); `scripts/generate_keypair.py` regenerates it — knowing that this
invalidates the copies consumers keep (e.g. `insights360/.env.example`).

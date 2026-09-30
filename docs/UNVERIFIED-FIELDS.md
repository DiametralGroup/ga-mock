---
type: registry
description: >
  Registry of mock fields and behaviors NOT attested against the real GA4 Data
  v1beta service. Every entry of the UNVERIFIED_BEHAVIORS tuple
  (src/ga_mock/models.py) and every x-ga-confidence marker in the contract
  MUST appear here — a test enforces it.
sources_of_truth:
  - https://developers.google.com/analytics/devguides/reporting/data/v1/rest
  - https://analyticsdata.googleapis.com/$discovery/rest?version=v1beta
  - https://developers.google.com/identity/protocols/oauth2/service-account
review_triggers:
  - a replay against a real GA4 property (scripts/compare_real.py)
  - any v1beta API version bump
update_policy: propose
last_verified: 2026-09-02
attested_elsewhere: docs/CONFORMITE-REELLE.md
---

# Unverified fields and behaviors

The rule comes from the insights360 spec, carried over from
boondmanager-mock: *never invent an API field without saying so*.

This registry was **two-thirds emptied on 2026-09-02** by a full replay
against a real GA4 property (`make compare` — 53 cases, 53 conforming; 21
error messages identical character for character). Everything that was
recorded moved to [CONFORMITE-REELLE.md](CONFORMITE-REELLE.md), which is now
authoritative. What remains below is what could NOT be observed, or what the
mock knowingly does differently.

## Behaviors (`UNVERIFIED_BEHAVIORS`)

### Not observable

- `messages-quota-429` — The three exhaustion messages (`property per day`,
  `property per hour`, `project per hour`) follow the bucket NAME as the
  discovery document defines it. Verifying it would mean actually exhausting
  a real property's quota for 24h: this will not be done.
- `cout-jetons-interpole` — A report's cost is not flat, and the mock's
  model (`1 + days//60 + ⌈(dims×metrics − 3)/32⌉`) reproduces the SEVEN
  measured points (see CONFORMITE-REELLE §3) but is not Google's formula,
  which surely also depends on cardinality and sampling.
- `total-avant-having` — `TOTAL` is computed BEFORE the `metricFilter`
  (exact user dedup requires it). The `RESERVED_TOTAL`/`RESERVED_MAX`/
  `RESERVED_MIN` markers themselves are attested; only the interaction with
  the having clause is not.
- `spine-keep-empty-rows` — `keepEmptyRows` completes the calendar when ALL
  dimensions are date-family. The replay property has data every day: the
  "genuinely empty day" case could not be distinguished. Other dimension
  families are not synthesized.
- `valeurs-vides-emptyfilter` — `emptyFilter` matches `""` and `(not set)`,
  the only two values NAMED by the reference. Other parenthesized markers
  (`(none)`, `(direct)`, `(organic)`, `(data not available)`) are treated as
  real values; the vendor's classification is not attested.
- `regle-semaine` — `week`: Sunday-Saturday weeks, week 01 starts on January 1st.
  The format (two digits) is attested; the exact edge rule for years with 54
  partial "weeks" is not.
- `fallback-session-campaign-name` — Organic-channel fallbacks: `(organic)`,
  `(direct)`, `(referral)`, `(not set)`. The real vocabulary is far broader
  (see `vocabulaire-sans-not-set`).

### Approximations and assumed gaps

- `suggestions-did-you-mean` — The service prefixes its field errors with a
  suggestion (`Did you mean fileExtension? Field … is not a valid
  dimension.`). The mock does NOT reproduce it: it is computed against
  Google's FULL catalog, which it does not serve. The rest of the message is
  identical character for character, spaces included.
- `raison-parseur-json` — On a syntactically broken JSON body, the message's
  GEOMETRY is faithful (reason, offending line, caret under the column) but
  the reason wording comes from Python's parser, not protobuf's C++ parser
  (`Expected : between key:value pair.`).
- `ordre-par-defaut-secondaire` — With no `orderBys`, sorting by first metric
  DESCENDING is attested. The secondary sort (dimensions ascending) is a
  mock choice: the vendor makes no promise about tie order, and a mock must
  never render a dict's iteration order.
- `fanout-inter-portees` — `pagePath`/`eventName` split the session into units;
  session-scope metrics stay exact distinct counts, but cross-attribution
  (duration per page, event per page) is approximate. The page x event cross
  product does not reproduce GA4's real attribution.
- `vocabulaire-sans-not-set` — The generated world NEVER produces
  `(not set)`, where the service renders it on almost every dimension
  (`deviceCategory`, `browser`, `operatingSystem`, `country`, `region`,
  `city`, `newVsReturning`…), nor the real catalog's rare values (`smart tv`,
  `Cross-network`, `AI Assistant`, `(data not available)`). A consumer that
  assumes `deviceCategory` is in `{desktop, mobile, tablet}` will pass here
  and break in prod. Fixing it requires MODIFYING THE GENERATED WORLD, hence
  invalidating consumers' pinned fixtures: open decision, see
  CONFORMITE-REELLE §5.
- `retry-after-sur-429` — The mock sets a `Retry-After` header on injected
  429s; the real service does not. Assumed test affordance.
- `aud-tolerant` — The mock accepts any audience ending in `/token` instead
  of requiring strict equality (behind compose, the client targets a
  different host than the one the server believes it is). The real behavior
  IS attested, including its message: this is an assumed gap, not an
  approximation — see CONFORMITE-REELLE §4.
- `fenetre-assertion-double-horloge` — The assertion's iat/exp window is accepted
  if valid against either the VIRTUAL clock (world anchor) OR the REAL clock:
  a real client signs at real time, test assertions are crafted against the
  anchor. An expired assertion fails against both. The real endpoint
  obviously has only one clock.

## `x-ga-confidence` markers in the contract

- `PropertyQuota.tokensPerDay` — see `cout-jetons-interpole`.
- `RunReportResponse.totals` — see `total-avant-having`.
- `FilterLeaf.emptyFilter` — see `valeurs-vides-emptyfilter`.

---
type: attestation
description: >
  Relevés faits SUR le service Google réel et verdicts de conformité du mock.
  Chaque ligne est une observation datée, reproductible par
  `scripts/compare_real.py` ; ce document est la source qui autorise à retirer
  une entrée de UNVERIFIED-FIELDS.md.
sources:
  - analyticsdata.googleapis.com — propriété GA4 réelle, compte de service
  - oauth2.googleapis.com/token — flux JWT-bearer, vraie clé privée
  - https://analyticsdata.googleapis.com/$discovery/rest?version=v1beta (rév. 20260831)
outil: scripts/compare_real.py
date_releve: 2026-09-02
resultat: 53/53 cas conformes ; 21 messages d'erreur identiques au caractère près
---

# Conformité relevée contre le service réel

Le mock a été confronté au vrai Google le **2026-09-02** : 53 cas de forme
(`make compare`) et une comparaison de vocabulaire (`make compare
ARGS=--vocabulaire`), plus une trentaine de sondes ciblées sur `/token` et la
passerelle. Trois sources, trois pouvoirs différents : le **document de
découverte** fait foi sur les FORMES (il est généré depuis les protos du
vendeur), les **relevés HTTP** font foi sur les comportements et les wordings,
et ce qui n'a pas pu être observé reste dans
[UNVERIFIED-FIELDS.md](UNVERIFIED-FIELDS.md).

Ce qui suit est le PV. Les lignes « corrigé » ont produit du code ; les lignes
« conforme » ont produit un test qui fige ce qui l'était déjà.

## 1. Le document de découverte (rév. 20260831)

| Constat | Mock avant | Verdict |
|---|---|---|
| `PropertyQuota` compte **six** seaux : `tokensPerProjectPerHour` manquait | 5 seaux | **corrigé** — seau ajouté, décompté et plafonné à 14 000 (35 % de l'horaire, règle écrite dans le discovery, valeur confirmée sur la vraie propriété) |
| `Filter.emptyFilter` existe | rejeté en 400 | **corrigé** — prédicat implémenté |
| `RunReportRequest` porte `currencyCode` | ignoré | **corrigé** — la valeur est renvoyée dans `metadata.currencyCode` |
| `RunReportRequest` porte `property`, `cohortSpec`, `comparisons` | `cohortSpec`/`comparisons` refusés | écart de PÉRIMÈTRE assumé, §4 |
| `ResponseMetaData` porte 5 champs de plus | absents | **conforme** — le service ne les rend pas non plus sur une réponse nominale (omission proto3) |

## 2. La passerelle `analyticsdata.googleapis.com`

Ces cas ne demandent NI l'activation de l'API NI de droits sur une propriété.

| Cas | Réel | Mock avant | Verdict |
|---|---|---|---|
| `POST :runReport` sans en-tête `Authorization` | `401`, *"Request is **missing** required authentication credential…"*, `details[0].reason = CREDENTIALS_MISSING` avec le nom gRPC de la méthode, `WWW-Authenticate: Bearer realm="https://accounts.google.com/"` | message « had invalid », pas de `details`, realm avec `error="invalid_token"` | **corrigé** — deux 401 distincts |
| `POST :runReport` avec un bearer bidon | `401`, *"Request **had invalid** authentication credentials…"*, **pas** de `details`, `WWW-Authenticate: …, error="invalid_token"` | identique | **conforme** |
| `POST …:runNothing` (verbe inconnu) | `404`, `text/html; charset=UTF-8` | enveloppe JSON `NOT_FOUND` | **corrigé** — page HTML |
| `GET /v1beta/pasUneRessource` | `404` HTML | enveloppe JSON | **corrigé** |
| `GET …:runReport` (mauvais verbe) | **`404`** HTML — jamais 405 | `405 METHOD_NOT_ALLOWED` | **corrigé** — `METHOD_NOT_ALLOWED` retiré de la table des statuts |
| corps JSON cassé sans bearer | `401` — l'auth précède le parsing | identique | **conforme** |

La page HTML est reproduite dans sa STRUCTURE (statut, `Content-Type`, titre
`Error 404 (Not Found)!!1`), pas au balisage près : ce qui compte est que
`reponse.json()` échoue. Elle est réservée aux chemins du vendeur — une faute
de frappe sur `/__admin` garde une erreur lisible.

## 3. La surface `runReport` / `batchRunReports` / `metadata`

### Corrigé

| Constat relevé | Mock avant |
|---|---|
| **Un filtre n'a PAS à porter sur un champ demandé** : filtrer sur `country` en groupant par `date` marche, et un `metricFilter` peut viser une métrique absente du rapport | 400 « must be a requested dimension » — un 400 en dev là où la prod répond 200 |
| **Un rapport SANS métrique est valide** : lignes de dimensions nues, pas de `metricHeaders`, pas de `metricValues`. Seule l'absence des DEUX est refusée | 400 « at least one metric » |
| **Les clés JSON inconnues sont refusées** par le transcodage, AVANT la méthode : `Invalid JSON payload received. Unknown name "x": Cannot find field.`, une `fieldViolation` par clé, message = leur concaténation. Les clés imbriquées nomment le chemin PROTO : `at 'date_ranges[0]'` | ignorées en silence |
| **Bornes de dates constantes** : `start_date` doit être STRICTEMENT postérieure au `2015-08-13` (le 13 lui-même est refusé) et antérieure au `3000-01-01` | aucune borne |
| **Sérialisation des doubles = `DoubleToBuffer` de protobuf** : `.15g`, et seulement s'il ne fait pas l'aller-retour, `.17g` — jamais 16. Le `repr` de Python donne `0.9734848484848485` là où le service donne `0.97348484848484851`. Règle vérifiée sur 21 valeurs | `repr()` |
| **`QuotaStatus` rend toujours ses deux champs**, `consumed: 0` compris — exception à la règle proto3 d'omission | zéros omis |
| **Le coût en jetons n'est pas forfaitaire** : 1 pour un rapport court et étroit, 2 pour 1 dim × 10 métriques, 4 pour 9 × 10, 7 pour 1 × 1 sur 365 jours | forfait de 10 |
| `metadata` : **`customDefinition` est ABSENT** quand il vaut false | rendu à `false` |
| `metadata` : `deprecatedApiNames` sur `dayOfWeek` (`dayOfWeekZero`), `sessionDefaultChannelGroup` (`sessionDefaultChannelGrouping`), `keyEvents` (`conversions`), `sessionKeyEventRate` (`sessionConversionRate`) | absent |
| `metadata` : catégorie `Traffic Source` (S majuscule), `newVsReturning` → `User`, `userEngagementDuration` → `User`, `sessionKeyEventRate` → `Session` | `Traffic source`, `User lifetime`, `Session`, `Event` |
| `metadata` : `sessionsPerUser` s'appelle **« Sessions per active user »** — le dénominateur est `activeUsers`, pas `totalUsers` | nom et formule erronés |
| `metadata` : neuf `comparisons` standard sur une propriété, AUCUNE sur `properties/0` | absent |
| **Un corps vide n'est pas une erreur de parsing** : il traverse en message vide et échoue sur `A dateRange is required.` | « Invalid JSON payload received. » |
| Racine non-objet (`null`, `[]`) → `Invalid JSON payload received. Unknown name "": Root element must be a message.` | même message générique |
| JSON cassé → message **multiligne** : raison, ligne fautive, caret sous la colonne | une seule ligne |
| **L'ORDRE des contrôles** : transcodage → validité des noms → `limit`/`offset` → plage de dates → bornes de cardinalité → « dimensions and/or metrics ». Dix dimensions SANS plage sortent sur `A dateRange is required.` | bornes avant la plage |
| **Tous les wordings de validation** : `A dateRange is required.`, `start_date must be less than or equal to end_date. start_date = … and end_date = …`, `Invalid startDate : …. startDate must be YYYY-MM-DD, NdaysAgo, yesterday, or today.`, `Requests are limited to 4 dateRanges.\n  This request contains N dateRanges.`, `… 9 dimensions within a nested request.`, `… 10 metrics within a nested request.`, `Found duplicate dimensions: X`, `Duplicate metrics are not allowed. Found duplicate metrics: X`, `limit must be positive. The API received limit = -1`, `offset must be positive. …`, `Field X exists in OrderBy but is not defined in input Dimensions/Metrics list`, `Metric aggregation Count is not supported in ReportRequest.`, `Invalid value at 'metric_aggregations[0]' (…MetricAggregation), "X"`, `Batch requests are limited to 5 requests.\n  This batch request contains N requests.`, `The batchRunReportsRequest must contain at least one runReportRequest.`, `Invalid property ID: abc. A numeric Property ID is required. …`, `Field X is not a valid dimension. For a list …` (UNE espace) et `… is not a valid metric.  For a list …` (DEUX espaces) | wordings inventés |

### Conforme — figé par un test

- Marqueurs d'agrégation `RESERVED_TOTAL` / `RESERVED_MAX` / `RESERVED_MIN`
  (et non `RESERVED_MAXIMUM`, malgré ce que suggère la doc).
- Dimension implicite `dateRange` ajoutée en DERNIÈRE position, plages sans nom
  étiquetées `date_range_0`, `date_range_1`…
- Ordre par défaut sans `orderBys` : première métrique DÉCROISSANTE.
- `limit: 0` retombe sur le défaut ; au-delà de 250 000, plafonné en silence ;
  `limit` négative refusée.
- int64 (`limit`, `offset`) accepté en nombre JSON OU en chaîne.
- Valeurs de métriques toujours en chaînes ; flottants entiers rendus sans
  `.0` ; `rows`/`rowCount` absents quand vides ; `kind` toujours présent.
- `keyEvents` est bien **TYPE_FLOAT** (c'était une hypothèse).
- Noms géographiques SANS accents (`Auvergne-Rhone-Alpes`,
  `Asnieres-sur-Seine`, `Baden-Wurttemberg`) — la seule exception relevée est
  `country`, où `Côte d'Ivoire` garde le sien.
- Formats : `date` en `YYYYMMDD`, `week`/`month` sur deux chiffres,
  `yearMonth` sur six, `dayOfWeek` de 0 à 6, `deviceCategory` en minuscules,
  `sessionDefaultChannelGroup` capitalisé.
- Les 20 dimensions et 15 métriques du mock existent TOUTES sur la vraie
  propriété, avec les mêmes types.

## 4. `oauth2.googleapis.com/token`

Ordre attesté : structure → signature → `iss` → présence de `iat`/`exp` →
fenêtre → `scope` vide → `aud`.

| Cas | Réel | Verdict |
|---|---|---|
| `grant_type` inconnu / absent | `400 unsupported_grant_type` / `Invalid grant_type: <valeur>` | **conforme** |
| assertion absente, vide, ≠ 3 segments, base64 invalide, segment non-JSON ou non-objet | `400` **`invalid_request`** / **`Bad Request`** — aucune explication | **corrigé** |
| signature fausse | `400 invalid_grant` / `Invalid JWT Signature.` | **conforme** |
| `alg` = HS256, `none`, ou absent | `400 invalid_grant` / **`Invalid JWT Signature.`** — pas de message dédié | **corrigé** |
| `iss` inconnu ou absent | `400 invalid_grant` / `Invalid grant: account not found` | **conforme** |
| `iat` / `exp` absents | messages dédiés : `Invalid JWT: iat (issued at) is not set.` / `… exp (expiration time) …` | **corrigé** |
| expirée / durée > 60 min / `iat` futur | le long message `short-lived token (60 minutes)` | **conforme**, au caractère près |
| `iat`/`exp` en **chaîne** de chiffres | `200` — accepté | **corrigé** |
| `scope` vide | `400 invalid_scope` / `Invalid OAuth scope or ID token audience provided.` | **conforme** |
| `scope` bien formé mais non reconnu, ou MIXTE (un valide + un inconnu) | **`200 {"id_token": …}`** — aucun `access_token` | **corrigé** |
| `aud` absent, autre hôte, ou sans `/token` | `400 invalid_grant` / **`Invalid JWT: Failed audience check.`** | **corrigé** (wording), tolérance maintenue — §5 |
| nominal | `200`, `access_token`, **`expires_in: 3599`**, `token_type: Bearer` | **conforme** — le 3599 était une hypothèse |

Claims de l'`id_token` réel, reproduits : `iss=https://accounts.google.com`,
`aud=<le scope demandé>`, `azp`/`email` = adresse du compte, `email_verified`,
`sub` = identifiant numérique, `iat`, `exp` ; en-tête `{alg, kid, typ}`.

## 5. Écarts ASSUMÉS

Le comportement réel est connu ; l'écart est un choix.

- **Catalogue.** Le mock sert 20 dimensions et 15 métriques ; la vraie
  propriété en annonce plusieurs centaines (dont des `TYPE_CURRENCY` et
  `TYPE_MILLISECONDS`). Le harnais compte ça « hors périmètre », pas comme un
  écart : ce que le mock déclare est exact, il en déclare simplement moins.
- **`aud` tolérant.** Le vendeur exige l'égalité stricte ; le mock accepte
  toute audience finissant par `/token` — derrière compose, l'assertion vise
  `http://ga-mock:8000/token`. Le refus emprunte le message réel.
- **Scopes « reconnus ».** La bascule vers `id_token` est attestée, mais le
  mock ne connaît que les deux scopes Analytics : un scope Google valide mais
  étranger obtient un `id_token` ici et un `access_token` chez Google.
- **`cohortSpec` / `comparisons` refusés en 400.** Le vendeur les accepte. Les
  ignorer en silence laisserait croire qu'ils ont agi.
- **Endpoints hors périmètre.** `runPivotReport`, `batchRunPivotReports`,
  `runRealtimeReport`, `checkCompatibility`, `audienceExports` rendent un 404
  de routage.
- **Préfixe des bearers.** `ya29.mock.` au lieu de `ya29.c.` : un jeton du
  mock doit rester reconnaissable dans un log.
- **Suggestions « Did you mean … ? »** non reproduites — elles se calculent
  sur le catalogue complet de Google.

## 6. Décision ouverte : le vocabulaire du monde généré

Le seul écart de fond qui n'a PAS été corrigé, parce qu'il touche le monde et
non le dialecte : **le mock ne produit jamais `(not set)`**, là où le service
en rend sur presque toutes les dimensions. Il ignore aussi `smart tv`,
`Cross-network`, `AI Assistant`, `(data not available)`, `referral_profile`…

Un consommateur qui écrit `if device == "mobile" … elif device == "tablet"`
sans branche par défaut passera contre le mock et cassera en prod.

Corriger demande de modifier `dataset/sessions.py`, donc de changer le monde à
seed constant — ce qui invalide toutes les fixtures épinglées chez les
consommateurs (insights360 en tête). C'est un arbitrage de publication, pas
une correction technique : il attend une décision.

## 7. Rejouer

```bash
GA_REAL_SA=/chemin/sa.json GA_REAL_PROPERTY=<id> make compare
GA_REAL_SA=… GA_REAL_PROPERTY=… make compare ARGS=--vocabulaire
```

Le compte de service a besoin de `analytics.readonly` sur la propriété, et les
APIs **Google Analytics Data** et **Admin** doivent être activées sur son
projet — sinon `403 SERVICE_DISABLED`, et le script sort en code 3 en le
disant.

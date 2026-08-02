---
type: registry
description: >
  Registre des champs et comportements du mock NON attestés contre la
  référence publique de l'API GA4 Data v1beta. Chaque entrée du tuple
  UNVERIFIED_BEHAVIORS (src/ga_mock/models.py) et chaque marqueur
  x-ga-confidence du contrat DOIVENT figurer ici — un test l'impose.
sources_of_truth:
  - https://developers.google.com/analytics/devguides/reporting/data/v1/rest
  - https://analyticsdata.googleapis.com/$discovery/rest?version=v1beta
  - https://developers.google.com/identity/protocols/oauth2/service-account
review_triggers:
  - un rejeu contre une vraie propriété GA4 (futur scripts/compare_real.py)
  - toute montée de version de l'API v1beta
update_policy: propose
last_verified: 2026-08-02
---

# Champs et comportements non vérifiés

La règle vient de la spec insights360, reprise de boondmanager-mock : *ne pas
inventer de champs d'API sans le dire*. Ce qui suit est plausible, cohérent et
testé — mais n'a pas été confronté au service réel. Un rejeu contre une vraie
propriété est LE moyen de purger ce registre.

## Comportements (`UNVERIFIED_BEHAVIORS`)

- `messages-erreurs-validation` — Les wordings des 400 de validation
  (`Field X is not a valid dimension.`, bornes `9 dimensions`/`10 metrics`,
  dates invalides, plage inversée, doublons) suivent la forme observée dans la
  documentation, pas une capture du service. Les codes et statuts (`400` /
  `INVALID_ARGUMENT`), eux, sont sûrs. Le vrai service ajoute des suggestions
  (« Did you mean … ? ») non reproduites.
- `messages-oauth-token` — Sur `/token`, `Invalid JWT Signature.` et
  `Invalid grant: account not found` sont des chaînes observées de longue date ;
  les autres descriptions (`unsupported_grant_type`, assertion malformée,
  fenêtre temporelle, `invalid_scope`) sont reconstituées.
- `ordre-par-defaut` — Sans `orderBys`, le mock trie première métrique
  décroissante puis dimensions croissantes. L'ordre par défaut réel n'est pas
  documenté ; le mock choisit un ordre DÉTERMINISTE plutôt que de singer un
  hasard.
- `regle-semaine` — `week` : semaines dimanche-samedi, la semaine 01 commence
  le 1ᵉʳ janvier. La règle de bord exacte (années à 54 « semaines »
  partielles ?) n'est pas attestée.
- `fallback-session-campaign-name` — Replis par canal organique :
  `(organic)`, `(direct)`, `(referral)`, `(not set)`. Les valeurs réelles par
  medium ne sont pas toutes attestées.
- `type-keyevents` — `keyEvents` est servi en TYPE_FLOAT (héritage du
  comptage fractionnaire de `conversions`) ; à confronter au service.
- `fanout-inter-portees` — `pagePath`/`eventName` éclatent la session en
  unités ; les métriques de portée session restent des dénombrements
  distincts exacts, mais l'attribution croisée (durée par page, événement par
  page) est approchée : le produit croisé page x événement ne reproduit pas
  l'attribution réelle de GA4.
- `marqueurs-reserved` — Lignes d'agrégation : `RESERVED_TOTAL` /
  `RESERVED_MAX` / `RESERVED_MIN` posés sur TOUTES les dimensions régulières ;
  `TOTAL` est calculé AVANT le metricFilter (dédoublonnage exact oblige),
  l'interaction réelle avec le having n'est pas attestée.
- `quota-cout-forfaitaire` — Coût forfaitaire de 10 jetons par rapport ; le
  vrai coût varie avec la complexité de la requête.
- `www-authenticate-realm` — La PRÉSENCE de `WWW-Authenticate` sur les 401
  est attestée ; la valeur exacte du realm/error ne l'est pas.
- `enveloppe-404-405` — Route inconnue → 404 NOT_FOUND, mauvais verbe → 405
  `METHOD_NOT_ALLOWED` (statut qui n'existe pas dans google.rpc) : politique
  du mock, le comportement de la passerelle réelle n'est pas attesté.
- `geo-sans-accents` — Noms géographiques anglais sans accents
  (`Ile-de-France`, `Auvergne-Rhone-Alpes`) ; la politique d'accents réelle de
  l'API n'est pas attestée.
- `retry-after-sur-429` — Le mock met un en-tête `Retry-After` sur les 429
  injectés ; le service réel n'en met probablement pas — l'en-tête est une
  affordance de test assumée.
- `aud-tolerant` — Le mock vérifie que `aud` se termine par `/token` au lieu
  d'exiger l'égalité stricte avec l'URL du endpoint (derrière compose, le
  client vise un autre hôte que celui que le serveur croit être).
- `limit-zero-defaut` — `limit: 0` retombe sur le défaut 10000 ; au-delà de
  250000, plafonné EN SILENCE (comportement documenté par Google, wording du
  bord non attesté).
- `position-dimension-daterange` — Avec 2 à 4 plages, la dimension implicite
  `dateRange` est ajoutée en DERNIÈRE position des en-têtes ; la position
  réelle n'est pas attestée.

## Marqueurs `x-ga-confidence` du contrat

- `RunReportRequest.limit` — voir `limit-zero-defaut`.
- `RunReportResponse.totals` — voir `marqueurs-reserved`.
- `PropertyQuota.tokensPerDay` — voir `quota-cout-forfaitaire`.

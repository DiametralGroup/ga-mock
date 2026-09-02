---
type: registry
description: >
  Registre des champs et comportements du mock NON attestés contre le service
  GA4 Data v1beta. Chaque entrée du tuple UNVERIFIED_BEHAVIORS
  (src/ga_mock/models.py) et chaque marqueur x-ga-confidence du contrat DOIVENT
  figurer ici — un test l'impose.
sources_of_truth:
  - https://developers.google.com/analytics/devguides/reporting/data/v1/rest
  - https://analyticsdata.googleapis.com/$discovery/rest?version=v1beta
  - https://developers.google.com/identity/protocols/oauth2/service-account
review_triggers:
  - un rejeu contre une vraie propriété GA4 (scripts/compare_real.py)
  - toute montée de version de l'API v1beta
update_policy: propose
last_verified: 2026-09-02
attested_elsewhere: docs/CONFORMITE-REELLE.md
---

# Champs et comportements non vérifiés

La règle vient de la spec insights360, reprise de boondmanager-mock : *ne pas
inventer de champs d'API sans le dire*.

Ce registre a été **vidé des deux tiers le 2026-09-02** par un rejeu complet
contre une vraie propriété GA4 (`make compare` — 53 cas, 53 conformes ; 21
messages d'erreur identiques au caractère près). Tout ce qui a été relevé est
passé dans [CONFORMITE-REELLE.md](CONFORMITE-REELLE.md), qui fait désormais
foi. Ce qui reste ci-dessous est ce qui n'a PAS pu être observé, ou ce que le
mock fait sciemment autrement.

## Comportements (`UNVERIFIED_BEHAVIORS`)

### Non observable

- `messages-quota-429` — Les trois messages d'épuisement (`property per day`,
  `property per hour`, `project per hour`) suivent le NOM du seau tel que le
  document de découverte le définit. Vérifier coûterait d'épuiser pour de vrai
  le quota d'une propriété réelle pour 24 h : ce ne sera pas fait.
- `cout-jetons-interpole` — Le coût d'un rapport n'est pas forfaitaire, et le
  modèle du mock (`1 + jours//60 + ⌈(dims×métriques − 3)/32⌉`) reproduit les
  SEPT points mesurés (cf. CONFORMITE-REELLE §3) mais n'est pas la formule de
  Google, qui dépend sûrement aussi de la cardinalité et de l'échantillonnage.
- `total-avant-having` — `TOTAL` est calculé AVANT le `metricFilter`
  (dédoublonnage exact des utilisateurs oblige). Les marqueurs
  `RESERVED_TOTAL`/`RESERVED_MAX`/`RESERVED_MIN`, eux, sont attestés ; c'est
  seulement l'interaction avec le having qui ne l'est pas.
- `spine-keep-empty-rows` — `keepEmptyRows` complète le calendrier quand
  TOUTES les dimensions sont de la famille date. La propriété de rejeu a des
  données tous les jours : le cas « jour réellement vide » n'a pas pu être
  distingué. Les autres familles de dimensions ne sont pas synthétisées.
- `valeurs-vides-emptyfilter` — `emptyFilter` retient `""` et `(not set)`, les
  deux seules valeurs NOMMÉES par la référence. Les autres marqueurs
  parenthésés (`(none)`, `(direct)`, `(organic)`, `(data not available)`) sont
  traités comme des valeurs réelles ; le classement du vendeur n'est pas
  attesté.
- `regle-semaine` — `week` : semaines dimanche-samedi, la semaine 01 commence
  le 1ᵉʳ janvier. Le format (deux chiffres) est attesté ; la règle de bord
  exacte des années à 54 « semaines » partielles ne l'est pas.
- `fallback-session-campaign-name` — Replis par canal organique :
  `(organic)`, `(direct)`, `(referral)`, `(not set)`. Le vocabulaire réel est
  bien plus large (cf. `vocabulaire-sans-not-set`).

### Approximations et écarts assumés

- `suggestions-did-you-mean` — Le service préfixe ses erreurs de champ d'une
  suggestion (`Did you mean fileExtension? Field … is not a valid dimension.`).
  Le mock ne la reproduit PAS : elle est calculée sur le catalogue COMPLET de
  Google, qu'il ne sert pas. Le reste du message est identique au caractère
  près, espaces compris.
- `raison-parseur-json` — Sur un corps JSON syntaxiquement cassé, la GÉOMÉTRIE
  du message est fidèle (raison, ligne fautive, caret sous la colonne) mais le
  libellé de la raison vient du parseur Python, pas du parseur C++ de protobuf
  (`Expected : between key:value pair.`).
- `ordre-par-defaut-secondaire` — Sans `orderBys`, le tri par première
  métrique DÉCROISSANTE est attesté. Le tri secondaire (dimensions
  croissantes) est un choix du mock : le vendeur ne promet aucun ordre à
  égalité, et un mock ne doit jamais rendre l'ordre d'un dict.
- `fanout-inter-portees` — `pagePath`/`eventName` éclatent la session en
  unités ; les métriques de portée session restent des dénombrements distincts
  exacts, mais l'attribution croisée (durée par page, événement par page) est
  approchée. Le produit croisé page x événement ne reproduit pas l'attribution
  réelle de GA4.
- `vocabulaire-sans-not-set` — Le monde généré ne produit JAMAIS `(not set)`,
  là où le service en rend sur presque toutes les dimensions
  (`deviceCategory`, `browser`, `operatingSystem`, `country`, `region`,
  `city`, `newVsReturning`…), ni les valeurs rares du vrai catalogue
  (`smart tv`, `Cross-network`, `AI Assistant`, `(data not available)`). Un
  consommateur qui suppose `deviceCategory` dans `{desktop, mobile, tablet}`
  passera ici et cassera en prod. Corriger demande de MODIFIER LE MONDE
  généré, donc d'invalider les fixtures épinglées des consommateurs : décision
  ouverte, cf. CONFORMITE-REELLE §5.
- `retry-after-sur-429` — Le mock met un en-tête `Retry-After` sur les 429
  injectés ; le service réel n'en met pas. Affordance de test assumée.
- `aud-tolerant` — Le mock accepte toute audience se terminant par `/token`
  au lieu d'exiger l'égalité stricte (derrière compose, le client vise un
  autre hôte que celui que le serveur croit être). Le comportement réel EST
  attesté, y compris son message : c'est un écart assumé, pas une
  approximation — cf. CONFORMITE-REELLE §4.
- `fenetre-assertion-double-horloge` — La fenêtre iat/exp de l'assertion est
  acceptée si elle est valide contre l'horloge VIRTUELLE (ancre du monde) OU
  contre l'horloge RÉELLE : un vrai client signe à l'heure réelle, les
  assertions de test sont fabriquées contre l'ancre. Une assertion périmée
  échoue contre les deux. Le vrai endpoint n'a évidemment qu'une horloge.

## Marqueurs `x-ga-confidence` du contrat

- `PropertyQuota.tokensPerDay` — voir `cout-jetons-interpole`.
- `RunReportResponse.totals` — voir `total-avant-having`.
- `FilterLeaf.emptyFilter` — voir `valeurs-vides-emptyfilter`.

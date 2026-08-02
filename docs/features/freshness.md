---
type: feature
description: >
  Le modèle de fraîcheur du mock : latence de traitement par session, jours
  récents partiels et monotones croissants, horloge virtuelle ancrée. C'est le
  levier qui permet aux consommateurs de tester leur ré-extraction des N
  derniers jours.
sources_of_truth:
  - src/ga_mock/dataset/sessions.py (lag_hours)
  - src/ga_mock/state.py (visible_sessions)
  - src/ga_mock/clock.py (ancre + offset)
review_triggers:
  - changement de GA_MOCK_FRESHNESS_HOURS par défaut
  - changement de l'ancre temporelle
update_policy: propose
last_verified: 2026-08-02
---

# Fraîcheur et horloge virtuelle

GA4 ne fige pas un jour à minuit : les données d'un jour récent continuent
d'arriver pendant ~48 h (traitement, hits tardifs). Un pipeline qui n'extrait
que « les nouvelles dates » sous-compte donc silencieusement les derniers
jours. Le mock reproduit ce piège pour que le pipeline apprenne à le déjouer.

## Le modèle

- Chaque session porte `lag_hours = FRESHNESS_HOURS x u^1.6` (u tiré au sort
  par session) : la plupart des sessions sont visibles en quelques heures, la
  queue s'étire jusqu'à la fenêtre complète.
- Une session n'est SERVIE que si `ts + lag_hours <= virtual_now()`.
- Conséquences : les jours sortis de la fenêtre sont complets et immuables ;
  aujourd'hui/hier grossissent de façon MONOTONE quand l'horloge avance ;
  l'historique ne se réécrit jamais (`build_day` est une fonction pure de
  (seed, jour)).

## L'horloge

`virtual_now() = ANCRE (2026-07-15T14:30+02:00) + offset`. La base est FIXE —
pas `time.time()` — parce que `today`/`NdaysAgo` font partie de la surface
d'API et doivent tomber dans le monde généré, et parce que l'ancre partage la
date de boondmanager-mock (jointures BI inter-sources cohérentes en dev).

Avancer : `POST /__admin/clock {"advance_seconds": 86400}`. L'avance fait
vieillir ENSEMBLE les dates relatives, l'expiration des bearers (TTL 3600 s
virtuelles — un client doit renouveler son jeton après une grande avance) et
la visibilité de fraîcheur. `POST /__admin/reset` ramène le temps à l'ancre.

## Le geste consommateur attendu

Ré-extraire une fenêtre glissante (>= 3 jours pour une fenêtre de 48 h) en
`merge` sur la clé de grain, jamais un simple « append des nouvelles dates ».
Le test de référence côté mock : `tests/test_freshness.py::
test_scenario_incremental_bout_en_bout`.

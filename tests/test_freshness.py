"""Le modèle de fraîcheur : les jours récents grossissent avec l'horloge.

C'est le remplaçant de l'`evolution.py` de boondmanager-mock : au lieu
d'événements scriptés, une latence de traitement par session (`lag_hours`).
Ce que ces tests prouvent, c'est exactement ce que le pipeline insights360
doit savoir absorber — ré-extraire une fenêtre récente donne PLUS de lignes,
jamais moins, et l'historique ne bouge jamais.

Avancer l'horloge fait AUSSI expirer les bearers (TTL 3600 s virtuelles) :
chaque lecture ré-obtient donc son jeton, exactement comme un vrai client qui
renouvelle. L'endpoint HTTP /__admin/clock est testé avec le plan de contrôle ;
ici on manipule l'horloge en direct (mode in-process).
"""

from datetime import date

import ga_mock
from ga_mock.clock import horloge
from ga_mock.dataset import build_day

SEED = 42
CHEMIN = "/v1beta/properties/424242001:runReport"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

UN_JOUR = 86_400


def _sessions_du_jour(client, jour: date) -> int:
    jeton = client.post(
        "/token", data={"grant_type": GRANT, "assertion": ga_mock.build_assertion()}
    ).json()["access_token"]
    reponse = client.post(
        CHEMIN,
        headers={"Authorization": f"Bearer {jeton}"},
        json={
            "dateRanges": [{"startDate": str(jour), "endDate": str(jour)}],
            "metrics": [{"name": "sessions"}],
        },
    )
    assert reponse.status_code == 200, reponse.text
    lignes = reponse.json().get("rows", [])
    return int(lignes[0]["metricValues"][0]["value"]) if lignes else 0


def test_aujourdhui_est_partiel(client):
    """À l'ancre (15 juillet, 14 h 30), le jour courant n'est pas fini d'être
    traité : le rapport doit en montrer STRICTEMENT moins que le monde n'en
    contient."""
    visibles = _sessions_du_jour(client, date(2026, 7, 15))
    total = len(build_day(SEED, date(2026, 7, 15)))
    assert 0 < visibles < total


def test_avancer_l_horloge_grossit_les_jours_recents(client):
    avant_15 = _sessions_du_jour(client, date(2026, 7, 15))
    avant_14 = _sessions_du_jour(client, date(2026, 7, 14))
    avant_08 = _sessions_du_jour(client, date(2026, 7, 8))

    horloge.offset_secondes += 6 * 3600

    apres_15 = _sessions_du_jour(client, date(2026, 7, 15))
    apres_14 = _sessions_du_jour(client, date(2026, 7, 14))
    apres_08 = _sessions_du_jour(client, date(2026, 7, 8))

    assert apres_15 >= avant_15
    assert apres_14 >= avant_14
    assert apres_15 + apres_14 > avant_15 + avant_14
    # un jour sorti de la fenêtre de fraîcheur est IMMUABLE
    assert apres_08 == avant_08 == len(build_day(SEED, date(2026, 7, 8)))


def test_apres_la_fenetre_le_jour_est_complet(client):
    horloge.offset_secondes += 3 * UN_JOUR
    for jour in (date(2026, 7, 14), date(2026, 7, 15)):
        assert _sessions_du_jour(client, jour) == len(build_day(SEED, jour))


def test_croissance_monotone(client):
    valeurs = [_sessions_du_jour(client, date(2026, 7, 15))]
    for _ in range(3):
        horloge.offset_secondes += 4 * 3600
        valeurs.append(_sessions_du_jour(client, date(2026, 7, 15)))
    assert valeurs == sorted(valeurs)


def test_determinisme_apres_reset(client):
    horloge.offset_secondes += UN_JOUR
    premier = _sessions_du_jour(client, date(2026, 7, 15))
    ga_mock.state.reset()
    horloge.offset_secondes += UN_JOUR
    second = _sessions_du_jour(client, date(2026, 7, 15))
    assert premier == second


def test_scenario_incremental_bout_en_bout(client):
    """Le geste du pipeline dlt : extraire une fenêtre, revenir le lendemain,
    RÉ-extraire la même fenêtre + le jour nouveau. Les jours récents ont
    grossi, l'historique n'a pas bougé, le jour nouveau existe."""
    fenetre = [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)]
    ancien = date(2026, 7, 10)

    extraction_1 = {j: _sessions_du_jour(client, j) for j in fenetre}
    ancien_1 = _sessions_du_jour(client, ancien)

    horloge.offset_secondes += UN_JOUR

    extraction_2 = {j: _sessions_du_jour(client, j) for j in fenetre}
    assert all(extraction_2[j] >= extraction_1[j] for j in fenetre)
    assert sum(extraction_2.values()) > sum(extraction_1.values())
    assert _sessions_du_jour(client, ancien) == ancien_1
    # le « nouveau jour » du monde est apparu, partiel mais non vide
    assert _sessions_du_jour(client, date(2026, 7, 16)) > 0

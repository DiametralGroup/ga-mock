"""Injection de pannes + plan de contrôle /__admin."""

import inspect
import time

import ga_mock
import ga_mock.app as app_module
from tests.conftest import ADMIN

CHEMIN = "/v1beta/properties/424242001:runReport"
CORPS = {
    "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
    "metrics": [{"name": "sessions"}],
}


def _injecter(client, **regle):
    reponse = client.post("/__admin/inject", headers=ADMIN, json=regle)
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def test_admin_exige_le_jeton(client):
    assert client.get("/__admin/state").status_code == 401
    faux = client.get("/__admin/state", headers={"X-Mock-Admin-Token": "faux"})
    assert faux.status_code == 401
    assert client.get("/__admin/state", headers=ADMIN).status_code == 200


def test_montage_conditionnel_atteste_dans_la_source():
    """Le contrat est que /__admin est ABSENT quand désactivé — pas monté puis
    interdit. La suite tournant avec l'admin activé, on atteste le mécanisme
    dans la source, comme boondmanager-mock."""
    assert "if settings.admin_enabled:" in inspect.getsource(app_module)


def test_rate_limit_apres_seuil(client, bearer):
    _injecter(
        client, kind="rate_limit", scope="*:runReport", after_requests=2, retry_after_seconds=3
    )
    for _ in range(2):
        assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200
    troisieme = client.post(CHEMIN, headers=bearer, json=CORPS)
    assert troisieme.status_code == 429
    assert troisieme.headers["Retry-After"] == "3"
    assert troisieme.json()["error"]["status"] == "RESOURCE_EXHAUSTED"
    # le scope ne touche pas /token : on peut toujours obtenir un jeton
    jeton = client.post(
        "/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": ga_mock.build_assertion(),
        },
    )
    assert jeton.status_code == 200


def test_status_transitoire_puis_retour_a_la_normale(client, bearer):
    _injecter(client, kind="status", scope="*:runReport", status=503, times=2)
    for _ in range(2):
        reponse = client.post(CHEMIN, headers=bearer, json=CORPS)
        assert reponse.status_code == 503
        assert reponse.json()["error"]["status"] == "UNAVAILABLE"
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200


def test_status_persistant_jusqu_au_clear(client, bearer):
    _injecter(client, kind="status", scope="*:runReport", status=500)
    for _ in range(3):
        assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 500
    assert client.post("/__admin/inject/clear", headers=ADMIN).status_code == 200
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200


def test_auth_reject_preempte_un_bearer_valide(client, bearer):
    regle = _injecter(client, kind="auth_reject", scope="*:runReport")
    refus = client.post(CHEMIN, headers=bearer, json=CORPS)
    assert refus.status_code == 401
    assert refus.json()["error"]["status"] == "UNAUTHENTICATED"
    suppression = client.delete(f"/__admin/inject/{regle['rule_id']}", headers=ADMIN)
    assert suppression.status_code == 200
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200


def test_suppression_regle_inconnue(client):
    assert client.delete("/__admin/inject/rule-999", headers=ADMIN).status_code == 404


def test_latency_ralentit_puis_s_epuise(client, bearer):
    _injecter(client, kind="latency", scope="*:runReport", seconds=0.15, times=1)
    debut = time.perf_counter()
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200
    assert time.perf_counter() - debut >= 0.15
    etat = client.get("/__admin/state", headers=ADMIN).json()
    assert etat["injections"] == []


def test_quota_exhausted_injecte(client, bearer):
    _injecter(client, kind="quota_exhausted", scope="*:runReport", times=1)
    reponse = client.post(CHEMIN, headers=bearer, json=CORPS)
    assert reponse.status_code == 429
    assert "per day" in reponse.json()["error"]["message"]
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200


def test_kind_inconnu_refuse(client):
    reponse = client.post("/__admin/inject", headers=ADMIN, json={"kind": "explosion"})
    assert reponse.status_code == 422


def test_reset_revient_au_baseline_de_l_environnement(client, bearer, monkeypatch):
    """Le reset ne revient pas « à vide » mais à la configuration de
    déploiement : une rate_limit posée par variable d'env doit survivre."""
    monkeypatch.setenv("GA_MOCK_RATE_LIMIT_AFTER", "1")
    reponse = client.post("/__admin/reset", headers=ADMIN, json={})
    assert reponse.json() == {"status": "reset", "seed": 42}
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 200
    assert client.post(CHEMIN, headers=bearer, json=CORPS).status_code == 429


def test_reset_change_le_monde_avec_la_seed(client, bearer):
    reference = client.post(CHEMIN, headers=bearer, json=CORPS).json()
    client.post("/__admin/reset", headers=ADMIN, json={"seed": 7})
    autre_monde = client.post(CHEMIN, headers=bearer, json=CORPS).json()
    assert autre_monde != reference
    client.post("/__admin/reset", headers=ADMIN, json={"seed": 42})
    retour = client.post(CHEMIN, headers=bearer, json=CORPS).json()
    assert retour == reference


def test_clock_endpoint_et_etat(client):
    avance = client.post("/__admin/clock", headers=ADMIN, json={"advance_seconds": 86_400})
    assert avance.status_code == 200
    assert avance.json()["virtual_now"].startswith("2026-07-16")
    etat = client.get("/__admin/state", headers=ADMIN).json()
    assert etat["clock_offset"] == 86_400
    recul = client.post("/__admin/clock", headers=ADMIN, json={"advance_seconds": -5})
    assert recul.status_code == 422


def test_state_note_le_dernier_corps_de_rapport(client, bearer):
    client.post(CHEMIN, headers=bearer, json=CORPS)
    etat = client.get("/__admin/state", headers=ADMIN).json()
    resume = etat["last_request_by_path"][CHEMIN]
    assert resume["dateRanges"] == CORPS["dateRanges"]
    assert resume["metrics"] == CORPS["metrics"]

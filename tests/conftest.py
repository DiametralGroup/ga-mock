"""Socle des tests.

L'environnement est posé AVANT l'import du package : la configuration est lue
à l'import (singleton `settings`), exactement comme boondmanager-mock. Poser
la variable après l'import ne testerait qu'un mock déjà configuré autrement.
"""

import os

os.environ.setdefault("GA_MOCK_ADMIN_ENABLED", "true")

import pytest
from fastapi.testclient import TestClient

import ga_mock

ADMIN = {"X-Mock-Admin-Token": "mock-admin-token"}
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


@pytest.fixture()
def client():
    c = TestClient(ga_mock.app)
    ga_mock.state.reset()
    yield c
    ga_mock.state.reset()


@pytest.fixture()
def bearer(client):
    """En-tête Authorization prêt à l'emploi — le flux token COMPLET, pas un
    passe-droit : si /token casse, toute la suite le voit immédiatement."""
    reponse = client.post(
        "/token", data={"grant_type": GRANT, "assertion": ga_mock.build_assertion()}
    )
    assert reponse.status_code == 200
    return {"Authorization": f"Bearer {reponse.json()['access_token']}"}

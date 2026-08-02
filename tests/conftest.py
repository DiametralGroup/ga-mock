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


@pytest.fixture()
def client():
    c = TestClient(ga_mock.app)
    ga_mock.state.reset()
    yield c
    ga_mock.state.reset()

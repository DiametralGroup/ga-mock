"""Test foundation.

The environment is set BEFORE the package import: config is read at import
time (the `settings` singleton), exactly like boondmanager-mock. Setting the
variable after import would only test a mock already configured differently.
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
    """Ready-to-use Authorization header — the FULL token flow, not a
    shortcut: if /token breaks, the whole suite sees it immediately."""
    reponse = client.post(
        "/token", data={"grant_type": GRANT, "assertion": ga_mock.build_assertion()}
    )
    assert reponse.status_code == 200
    return {"Authorization": f"Bearer {reponse.json()['access_token']}"}

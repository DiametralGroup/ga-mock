"""Failure injection + /__admin control plane."""

import inspect
import sys
import time

import ga_mock
from tests.conftest import ADMIN

PATH = "/v1beta/properties/424242001:runReport"
BODY = {
    "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
    "metrics": [{"name": "sessions"}],
}


def _inject(client, **rule):
    response = client.post("/__admin/inject", headers=ADMIN, json=rule)
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_requires_the_token(client):
    assert client.get("/__admin/state").status_code == 401
    fake = client.get("/__admin/state", headers={"X-Mock-Admin-Token": "fake"})
    assert fake.status_code == 401
    assert client.get("/__admin/state", headers=ADMIN).status_code == 200


def test_conditional_mounting_attested_in_the_source():
    """The contract is that /__admin is ABSENT when disabled — not mounted
    then forbidden. Since the suite runs with admin enabled, the mechanism
    is attested in the source, like boondmanager-mock."""
    # sys.modules and not `ga_mock.app`: the attribute gets reassigned to
    # the FastAPI instance by the package's __init__, the module stays here.
    assert "if settings.admin_enabled:" in inspect.getsource(sys.modules["ga_mock.app"])


def test_rate_limit_after_threshold(client, bearer):
    _inject(client, kind="rate_limit", scope="*:runReport", after_requests=2, retry_after_seconds=3)
    for _ in range(2):
        assert client.post(PATH, headers=bearer, json=BODY).status_code == 200
    third = client.post(PATH, headers=bearer, json=BODY)
    assert third.status_code == 429
    assert third.headers["Retry-After"] == "3"
    assert third.json()["error"]["status"] == "RESOURCE_EXHAUSTED"
    # the scope doesn't touch /token: a token can still be obtained
    token = client.post(
        "/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": ga_mock.build_assertion(),
        },
    )
    assert token.status_code == 200


def test_transient_status_then_back_to_normal(client, bearer):
    _inject(client, kind="status", scope="*:runReport", status=503, times=2)
    for _ in range(2):
        response = client.post(PATH, headers=bearer, json=BODY)
        assert response.status_code == 503
        assert response.json()["error"]["status"] == "UNAVAILABLE"
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 200


def test_persistent_status_until_clear(client, bearer):
    _inject(client, kind="status", scope="*:runReport", status=500)
    for _ in range(3):
        assert client.post(PATH, headers=bearer, json=BODY).status_code == 500
    assert client.post("/__admin/inject/clear", headers=ADMIN).status_code == 200
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 200


def test_auth_reject_preempts_a_valid_bearer(client, bearer):
    rule = _inject(client, kind="auth_reject", scope="*:runReport")
    refused = client.post(PATH, headers=bearer, json=BODY)
    assert refused.status_code == 401
    assert refused.json()["error"]["status"] == "UNAUTHENTICATED"
    deletion = client.delete(f"/__admin/inject/{rule['rule_id']}", headers=ADMIN)
    assert deletion.status_code == 200
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 200


def test_delete_unknown_rule(client):
    assert client.delete("/__admin/inject/rule-999", headers=ADMIN).status_code == 404


def test_latency_slows_down_then_runs_out(client, bearer):
    _inject(client, kind="latency", scope="*:runReport", seconds=0.15, times=1)
    start = time.perf_counter()
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 200
    assert time.perf_counter() - start >= 0.15
    state = client.get("/__admin/state", headers=ADMIN).json()
    assert state["injections"] == []


def test_quota_exhausted_injected(client, bearer):
    _inject(client, kind="quota_exhausted", scope="*:runReport", times=1)
    response = client.post(PATH, headers=bearer, json=BODY)
    assert response.status_code == 429
    assert "per day" in response.json()["error"]["message"]
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 200


def test_unknown_kind_rejected(client):
    response = client.post("/__admin/inject", headers=ADMIN, json={"kind": "explosion"})
    assert response.status_code == 422


def test_reset_goes_back_to_the_environment_baseline(client, bearer, monkeypatch):
    """Reset doesn't go back "empty" but to the deployment configuration: a
    rate_limit set via env var must survive."""
    monkeypatch.setenv("GA_MOCK_RATE_LIMIT_AFTER", "1")
    response = client.post("/__admin/reset", headers=ADMIN, json={})
    assert response.json() == {"status": "reset", "seed": 42}
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 200
    assert client.post(PATH, headers=bearer, json=BODY).status_code == 429


def test_reset_changes_the_world_with_the_seed(client, bearer):
    reference = client.post(PATH, headers=bearer, json=BODY).json()
    client.post("/__admin/reset", headers=ADMIN, json={"seed": 7})
    other_world = client.post(PATH, headers=bearer, json=BODY).json()
    assert other_world != reference
    client.post("/__admin/reset", headers=ADMIN, json={"seed": 42})
    back = client.post(PATH, headers=bearer, json=BODY).json()
    assert back == reference


def test_clock_endpoint_and_state(client):
    advance = client.post("/__admin/clock", headers=ADMIN, json={"advance_seconds": 86_400})
    assert advance.status_code == 200
    assert advance.json()["virtual_now"].startswith("2026-07-16")
    state = client.get("/__admin/state", headers=ADMIN).json()
    assert state["clock_offset"] == 86_400
    backward = client.post("/__admin/clock", headers=ADMIN, json={"advance_seconds": -5})
    assert backward.status_code == 422


def test_state_records_the_last_report_body(client, bearer):
    client.post(PATH, headers=bearer, json=BODY)
    state = client.get("/__admin/state", headers=ADMIN).json()
    summary = state["last_request_by_path"][PATH]
    assert summary["dateRanges"] == BODY["dateRanges"]
    assert summary["metrics"] == BODY["metrics"]

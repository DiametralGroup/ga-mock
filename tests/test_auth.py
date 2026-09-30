"""Authentication dialect.

`/token` speaks RFC 6749 (`error`/`error_description`); the Data surface
speaks google.rpc (`{"error": {code, message, status}}`). Both halves are
tested, including bearer aging via the virtual clock.
"""

import json

from ga_mock import keypair
from ga_mock.auth import build_assertion, fixture_service_account
from ga_mock.clock import clock
from ga_mock.rsa_min import b64url, b64url_decode, sign, verify

GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _request_token(client, assertion):
    return client.post("/token", data={"grant_type": GRANT, "assertion": assertion})


def _bearer(client):
    response = _request_token(client, build_assertion())
    assert response.status_code == 200
    return response.json()["access_token"]


def test_token_flow_nominal(client):
    response = _request_token(client, build_assertion())
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"].startswith("ya29.mock.")
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3599


def test_bearer_accepted_on_the_data_surface(client):
    token = _bearer(client)
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code != 401


def test_claims_rewritten_under_valid_signature_rejected(client):
    """An assertion whose claims are swapped while keeping the signature must
    fall on « Invalid JWT Signature. » — this is THE test that proves the
    signature is verified, not merely parsed."""
    header, payload, signature = build_assertion().split(".")
    claims = json.loads(b64url_decode(payload))
    claims["iss"] = "attacker@example.test"
    forged = f"{header}.{b64url(json.dumps(claims).encode())}.{signature}"
    response = _request_token(client, forged)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_grant"
    assert body["error_description"] == "Invalid JWT Signature."


def test_corrupted_signature_rejected(client):
    assertion = build_assertion()
    corrupted = assertion[:-4] + ("AAAA" if not assertion.endswith("AAAA") else "BBBB")
    response = _request_token(client, corrupted)
    assert response.status_code == 400
    assert response.json()["error_description"] == "Invalid JWT Signature."


def test_unknown_iss(client):
    response = _request_token(client, build_assertion(iss="other@example.test"))
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_grant"
    assert body["error_description"] == "Invalid grant: account not found"


def test_expired_assertion(client):
    from ga_mock.clock import virtual_now

    past = int(virtual_now().timestamp()) - 7200
    response = _request_token(client, build_assertion(iat=past))
    assert response.status_code == 400
    assert "short-lived" in response.json()["error_description"]


def test_excessive_lifetime(client):
    response = _request_token(client, build_assertion(lifetime=7200))
    assert response.status_code == 400
    assert "short-lived" in response.json()["error_description"]


def test_unrecognized_scope_yields_an_id_token_not_an_error(client):
    """THE service-account flow trap, found on the real endpoint: a
    well-formed but unrecognized scope does NOT produce an error — 200, and
    an `id_token` ALONE. The client that reads `["access_token"]` breaks here
    just like in prod."""
    response = _request_token(
        client, build_assertion(scope="https://www.googleapis.com/auth/cloud-platform")
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id_token"}
    header, payload, _ = body["id_token"].split(".")
    assert json.loads(b64url_decode(header))["kid"] == keypair.PRIVATE_KEY_ID
    claims = json.loads(b64url_decode(payload))
    assert claims["iss"] == "https://accounts.google.com"
    assert claims["aud"] == "https://www.googleapis.com/auth/cloud-platform"
    assert claims["email_verified"] is True
    # `sub` and the service account JSON's `client_id` are the SAME number.
    assert claims["sub"] == fixture_service_account("http://x")["client_id"]


def test_mixed_scope_also_switches_to_id_token(client):
    """A single unrecognized scope is enough, even alongside a valid one."""
    mixed = "https://www.googleapis.com/auth/analytics.readonly https://example.test/x"
    response = _request_token(client, build_assertion(scope=mixed))
    assert response.status_code == 200
    assert set(response.json()) == {"id_token"}


def test_empty_scope_is_a_real_error(client):
    response = _request_token(client, build_assertion(scope=""))
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_scope"
    assert body["error_description"] == "Invalid OAuth scope or ID token audience provided."


def test_invalid_aud(client):
    response = _request_token(client, build_assertion(aud="http://localhost:8012/other"))
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_grant"
    assert body["error_description"] == "Invalid JWT: Failed audience check."


def test_iat_and_exp_accepted_as_strings(client):
    """The vendor accepts `"iat": "1788361815"` — reproduced JSON laxity."""
    header, payload, _ = build_assertion().split(".")
    claims = json.loads(b64url_decode(payload))
    claims["iat"], claims["exp"] = str(claims["iat"]), str(claims["exp"])
    payload = b64url(json.dumps(claims).encode())
    signature = b64url(sign(f"{header}.{payload}".encode(), keypair.N, keypair.D))
    response = _request_token(client, f"{header}.{payload}.{signature}")
    assert response.status_code == 200
    assert "access_token" in response.json()


def test_missing_iat_or_exp_have_their_own_message(client):
    for key, expected in (
        ("iat", "Invalid JWT: iat (issued at) is not set."),
        ("exp", "Invalid JWT: exp (expiration time) is not set."),
    ):
        header, payload, _ = build_assertion().split(".")
        claims = json.loads(b64url_decode(payload))
        del claims[key]
        payload = b64url(json.dumps(claims).encode())
        signature = b64url(sign(f"{header}.{payload}".encode(), keypair.N, keypair.D))
        response = _request_token(client, f"{header}.{payload}.{signature}")
        assert response.status_code == 400
        assert response.json()["error_description"] == expected


def test_invalid_grant_type(client):
    response = client.post(
        "/token", data={"grant_type": "client_credentials", "assertion": build_assertion()}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


def test_undecodable_assertion_is_a_terse_invalid_request(client):
    """The vendor does NOT say what is wrong with an unreadable assertion: no
    « 3 segments expected », no offending segment — `invalid_request` and
    « Bad Request », period. A chattier mock would train the consumer toward
    a diagnosis it will never get."""
    for broken in ("not-a-jwt", "", "aaa.bbb", "@@@.###.$$$"):
        response = _request_token(client, broken)
        assert response.status_code == 400, broken
        body = response.json()
        assert body["error"] == "invalid_request", broken
        assert body["error_description"] == "Bad Request", broken


def test_non_object_json_segment_is_also_bad_request(client):
    header, _, signature = build_assertion().split(".")
    response = _request_token(client, f"{header}.{b64url(b'42')}.{signature}")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_non_rs256_alg_comes_out_as_a_fake_signature(client):
    """The vendor doesn't distinguish « wrong algorithm » from « wrong
    signature »: both produce `Invalid JWT Signature.`"""
    _, payload, signature = build_assertion().split(".")
    for raw_header in ({"alg": "HS256", "typ": "JWT"}, {"alg": "none"}, {"typ": "JWT"}):
        header = b64url(json.dumps(raw_header).encode())
        response = _request_token(client, f"{header}.{payload}.{signature}")
        assert response.status_code == 400
        assert response.json()["error_description"] == "Invalid JWT Signature."


def test_401_missing_credential_versus_401_rejected_token(client):
    """TWO distinct 401s, found on the real service.

    Missing header: « is missing required authentication credential », a
    `details` google.rpc.ErrorInfo `CREDENTIALS_MISSING`, and a
    `WWW-Authenticate` WITHOUT `error=`. Token present but rejected: « had
    invalid authentication credentials », no `details`, and
    `error="invalid_token"`. This is what a client uses to decide between
    "authenticate" and "refresh".
    """
    missing = client.post("/v1beta/properties/424242001:runReport", json={})
    assert missing.status_code == 401
    body = missing.json()["error"]
    assert body["status"] == "UNAUTHENTICATED"
    assert body["message"].startswith("Request is missing required authentication credential.")
    detail = body["details"][0]
    assert detail["reason"] == "CREDENTIALS_MISSING"
    assert detail["domain"] == "googleapis.com"
    assert detail["metadata"]["method"].endswith("BetaAnalyticsData.RunReport")
    assert missing.headers["WWW-Authenticate"] == 'Bearer realm="https://accounts.google.com/"'

    rejected = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": "Bearer whatever"},
        json={},
    )
    assert rejected.status_code == 401
    body = rejected.json()["error"]
    assert body["message"].startswith("Request had invalid authentication credentials.")
    assert "details" not in body
    assert 'error="invalid_token"' in rejected.headers["WWW-Authenticate"]


def test_rpc_method_named_by_endpoint(client):
    """`details` carries the full gRPC name of the TARGETED method."""
    batch = client.post("/v1beta/properties/424242001:batchRunReports", json={})
    meta = client.get("/v1beta/properties/424242001/metadata")
    assert batch.json()["error"]["details"][0]["metadata"]["method"].endswith("BatchRunReports")
    assert meta.json()["error"]["details"][0]["metadata"]["method"].endswith("GetMetadata")


def test_bearer_expires_with_the_clock(client):
    token = _bearer(client)
    clock.offset_seconds += 3601
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 401


def test_fixture_service_account(client):
    response = client.get("/__fixtures/service-account.json")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "service_account"
    assert body["client_email"].endswith("gserviceaccount.example")
    assert body["token_uri"] == "http://testserver/token"
    assert body["private_key"].startswith("-----BEGIN PRIVATE KEY-----")


def test_rsa_min_roundtrips():
    message = b"round-trip ga-mock"
    signature = sign(message, keypair.N, keypair.D)
    assert verify(message, signature, keypair.N, keypair.E)
    assert not verify(b"other message", signature, keypair.N, keypair.E)
    corrupted = bytes([signature[0] ^ 1]) + signature[1:]
    assert not verify(message, corrupted, keypair.N, keypair.E)

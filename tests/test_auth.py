"""Dialecte d'authentification.

`/token` parle RFC 6749 (`error`/`error_description`) ; la surface Data parle
google.rpc (`{"error": {code, message, status}}`). Les deux moitiés sont
testées, y compris le vieillissement des bearers par l'horloge virtuelle.
"""

import json

from ga_mock import keypair
from ga_mock.auth import build_assertion
from ga_mock.clock import horloge
from ga_mock.rsa_min import b64url, b64url_decode, sign, verify

GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _demander_token(client, assertion):
    return client.post("/token", data={"grant_type": GRANT, "assertion": assertion})


def _bearer(client):
    reponse = _demander_token(client, build_assertion())
    assert reponse.status_code == 200
    return reponse.json()["access_token"]


def test_flux_token_nominal(client):
    reponse = _demander_token(client, build_assertion())
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["access_token"].startswith("ya29.mock.")
    assert corps["token_type"] == "Bearer"
    assert corps["expires_in"] == 3599


def test_bearer_accepte_sur_la_surface_data(client):
    jeton = _bearer(client)
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": f"Bearer {jeton}"},
        json={},
    )
    assert reponse.status_code != 401


def test_claims_reecrits_sous_signature_valide_rejetes(client):
    """Une assertion dont on remplace les claims en gardant la signature doit
    tomber sur « Invalid JWT Signature. » — c'est LE test qui prouve que la
    signature est vérifiée, pas seulement parsée."""
    entete, charge, signature = build_assertion().split(".")
    claims = json.loads(b64url_decode(charge))
    claims["iss"] = "attaquant@exemple.test"
    forge = f"{entete}.{b64url(json.dumps(claims).encode())}.{signature}"
    reponse = _demander_token(client, forge)
    assert reponse.status_code == 400
    corps = reponse.json()
    assert corps["error"] == "invalid_grant"
    assert corps["error_description"] == "Invalid JWT Signature."


def test_signature_corrompue_rejetee(client):
    assertion = build_assertion()
    corrompue = assertion[:-4] + ("AAAA" if not assertion.endswith("AAAA") else "BBBB")
    reponse = _demander_token(client, corrompue)
    assert reponse.status_code == 400
    assert reponse.json()["error_description"] == "Invalid JWT Signature."


def test_iss_inconnu(client):
    reponse = _demander_token(client, build_assertion(iss="autre@exemple.test"))
    assert reponse.status_code == 400
    corps = reponse.json()
    assert corps["error"] == "invalid_grant"
    assert corps["error_description"] == "Invalid grant: account not found"


def test_assertion_expiree(client):
    from ga_mock.clock import virtual_now

    passe = int(virtual_now().timestamp()) - 7200
    reponse = _demander_token(client, build_assertion(iat=passe))
    assert reponse.status_code == 400
    assert "short-lived" in reponse.json()["error_description"]


def test_duree_excessive(client):
    reponse = _demander_token(client, build_assertion(lifetime=7200))
    assert reponse.status_code == 400
    assert "short-lived" in reponse.json()["error_description"]


def test_scope_invalide(client):
    reponse = _demander_token(
        client, build_assertion(scope="https://www.googleapis.com/auth/cloud-platform")
    )
    assert reponse.status_code == 400
    assert reponse.json()["error"] == "invalid_scope"


def test_aud_invalide(client):
    reponse = _demander_token(client, build_assertion(aud="http://localhost:8012/autre"))
    assert reponse.status_code == 400
    assert "aud" in reponse.json()["error_description"]


def test_grant_type_invalide(client):
    reponse = client.post(
        "/token", data={"grant_type": "client_credentials", "assertion": build_assertion()}
    )
    assert reponse.status_code == 400
    assert reponse.json()["error"] == "unsupported_grant_type"


def test_assertion_malformee(client):
    reponse = _demander_token(client, "pas-un-jwt")
    assert reponse.status_code == 400
    assert reponse.json()["error_description"].startswith("Invalid JWT")


def test_alg_hs256_rejete(client):
    entete = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    _, charge, signature = build_assertion().split(".")
    reponse = _demander_token(client, f"{entete}.{charge}.{signature}")
    assert reponse.status_code == 400
    assert "RS256" in reponse.json()["error_description"]


def test_sans_bearer_enveloppe_google_401(client):
    reponse = client.post("/v1beta/properties/424242001:runReport", json={})
    assert reponse.status_code == 401
    corps = reponse.json()
    assert corps["error"]["status"] == "UNAUTHENTICATED"
    assert "authentication credential" in corps["error"]["message"]
    assert "WWW-Authenticate" in reponse.headers


def test_bearer_bidon(client):
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": "Bearer nimporte-quoi"},
        json={},
    )
    assert reponse.status_code == 401


def test_bearer_expire_avec_l_horloge(client):
    jeton = _bearer(client)
    horloge.offset_secondes += 3601
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": f"Bearer {jeton}"},
        json={},
    )
    assert reponse.status_code == 401


def test_fixture_service_account(client):
    reponse = client.get("/__fixtures/service-account.json")
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["type"] == "service_account"
    assert corps["client_email"].endswith("gserviceaccount.example")
    assert corps["token_uri"] == "http://testserver/token"
    assert corps["private_key"].startswith("-----BEGIN PRIVATE KEY-----")


def test_rsa_min_coherent():
    message = b"aller-retour ga-mock"
    signature = sign(message, keypair.N, keypair.D)
    assert verify(message, signature, keypair.N, keypair.E)
    assert not verify(b"autre message", signature, keypair.N, keypair.E)
    corrompue = bytes([signature[0] ^ 1]) + signature[1:]
    assert not verify(message, corrompue, keypair.N, keypair.E)

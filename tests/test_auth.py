"""Dialecte d'authentification.

`/token` parle RFC 6749 (`error`/`error_description`) ; la surface Data parle
google.rpc (`{"error": {code, message, status}}`). Les deux moitiés sont
testées, y compris le vieillissement des bearers par l'horloge virtuelle.
"""

import json

from ga_mock import keypair
from ga_mock.auth import build_assertion, fixture_service_account
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


def test_scope_non_reconnu_rend_un_id_token_pas_une_erreur(client):
    """LE piège du flux service account, relevé sur le vrai endpoint : un scope
    bien formé mais non reconnu ne produit PAS d'erreur — 200, et un `id_token`
    SEUL. Le client qui lit `["access_token"]` casse ici comme en prod."""
    reponse = _demander_token(
        client, build_assertion(scope="https://www.googleapis.com/auth/cloud-platform")
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert set(corps) == {"id_token"}
    entete, charge, _ = corps["id_token"].split(".")
    assert json.loads(b64url_decode(entete))["kid"] == keypair.PRIVATE_KEY_ID
    claims = json.loads(b64url_decode(charge))
    assert claims["iss"] == "https://accounts.google.com"
    assert claims["aud"] == "https://www.googleapis.com/auth/cloud-platform"
    assert claims["email_verified"] is True
    # `sub` et le `client_id` du JSON de compte de service sont le MÊME nombre.
    assert claims["sub"] == fixture_service_account("http://x")["client_id"]


def test_scope_mixte_bascule_aussi_en_id_token(client):
    """Un seul scope inconnu suffit, même accompagné d'un scope valide."""
    melange = "https://www.googleapis.com/auth/analytics.readonly https://exemple.test/x"
    reponse = _demander_token(client, build_assertion(scope=melange))
    assert reponse.status_code == 200
    assert set(reponse.json()) == {"id_token"}


def test_scope_vide_est_une_vraie_erreur(client):
    reponse = _demander_token(client, build_assertion(scope=""))
    assert reponse.status_code == 400
    corps = reponse.json()
    assert corps["error"] == "invalid_scope"
    assert corps["error_description"] == "Invalid OAuth scope or ID token audience provided."


def test_aud_invalide(client):
    reponse = _demander_token(client, build_assertion(aud="http://localhost:8012/autre"))
    assert reponse.status_code == 400
    corps = reponse.json()
    assert corps["error"] == "invalid_grant"
    assert corps["error_description"] == "Invalid JWT: Failed audience check."


def test_iat_et_exp_acceptes_en_chaine(client):
    """Le vendeur accepte `"iat": "1788361815"` — laxisme JSON reproduit."""
    entete, charge, _ = build_assertion().split(".")
    claims = json.loads(b64url_decode(charge))
    claims["iat"], claims["exp"] = str(claims["iat"]), str(claims["exp"])
    charge = b64url(json.dumps(claims).encode())
    signature = b64url(sign(f"{entete}.{charge}".encode(), keypair.N, keypair.D))
    reponse = _demander_token(client, f"{entete}.{charge}.{signature}")
    assert reponse.status_code == 200
    assert "access_token" in reponse.json()


def test_iat_ou_exp_absents_ont_leur_propre_message(client):
    for cle, attendu in (
        ("iat", "Invalid JWT: iat (issued at) is not set."),
        ("exp", "Invalid JWT: exp (expiration time) is not set."),
    ):
        entete, charge, _ = build_assertion().split(".")
        claims = json.loads(b64url_decode(charge))
        del claims[cle]
        charge = b64url(json.dumps(claims).encode())
        signature = b64url(sign(f"{entete}.{charge}".encode(), keypair.N, keypair.D))
        reponse = _demander_token(client, f"{entete}.{charge}.{signature}")
        assert reponse.status_code == 400
        assert reponse.json()["error_description"] == attendu


def test_grant_type_invalide(client):
    reponse = client.post(
        "/token", data={"grant_type": "client_credentials", "assertion": build_assertion()}
    )
    assert reponse.status_code == 400
    assert reponse.json()["error"] == "unsupported_grant_type"


def test_assertion_indecodable_est_un_invalid_request_laconique(client):
    """Le vendeur ne DIT PAS ce qui cloche dans une assertion illisible : pas
    de « 3 segments attendus », pas de segment fautif — `invalid_request` et
    « Bad Request », point. Un mock plus bavard entraînerait le consommateur à
    un diagnostic qu'il n'aura jamais."""
    for cassee in ("pas-un-jwt", "", "aaa.bbb", "@@@.###.$$$"):
        reponse = _demander_token(client, cassee)
        assert reponse.status_code == 400, cassee
        corps = reponse.json()
        assert corps["error"] == "invalid_request", cassee
        assert corps["error_description"] == "Bad Request", cassee


def test_segment_json_non_objet_est_aussi_bad_request(client):
    entete, _, signature = build_assertion().split(".")
    reponse = _demander_token(client, f"{entete}.{b64url(b'42')}.{signature}")
    assert reponse.status_code == 400
    assert reponse.json()["error"] == "invalid_request"


def test_alg_non_rs256_sort_comme_une_signature_fausse(client):
    """Le vendeur ne distingue pas « mauvais algorithme » de « mauvaise
    signature » : les deux rendent `Invalid JWT Signature.`"""
    _, charge, signature = build_assertion().split(".")
    for entete_brut in ({"alg": "HS256", "typ": "JWT"}, {"alg": "none"}, {"typ": "JWT"}):
        entete = b64url(json.dumps(entete_brut).encode())
        reponse = _demander_token(client, f"{entete}.{charge}.{signature}")
        assert reponse.status_code == 400
        assert reponse.json()["error_description"] == "Invalid JWT Signature."


def test_401_credential_manquant_contre_401_jeton_refuse(client):
    """DEUX 401 distincts, relevés sur le vrai service.

    En-tête absent : « is missing required authentication credential », un
    `details` google.rpc.ErrorInfo `CREDENTIALS_MISSING`, et un
    `WWW-Authenticate` SANS `error=`. Jeton présent mais refusé : « had invalid
    authentication credentials », pas de `details`, et `error="invalid_token"`.
    C'est là-dessus qu'un client décide entre « s'authentifier » et
    « renouveler ».
    """
    absent = client.post("/v1beta/properties/424242001:runReport", json={})
    assert absent.status_code == 401
    corps = absent.json()["error"]
    assert corps["status"] == "UNAUTHENTICATED"
    assert corps["message"].startswith("Request is missing required authentication credential.")
    detail = corps["details"][0]
    assert detail["reason"] == "CREDENTIALS_MISSING"
    assert detail["domain"] == "googleapis.com"
    assert detail["metadata"]["method"].endswith("BetaAnalyticsData.RunReport")
    assert absent.headers["WWW-Authenticate"] == 'Bearer realm="https://accounts.google.com/"'

    refuse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers={"Authorization": "Bearer nimporte-quoi"},
        json={},
    )
    assert refuse.status_code == 401
    corps = refuse.json()["error"]
    assert corps["message"].startswith("Request had invalid authentication credentials.")
    assert "details" not in corps
    assert 'error="invalid_token"' in refuse.headers["WWW-Authenticate"]


def test_methode_rpc_nommee_par_endpoint(client):
    """Le `details` porte le nom gRPC complet de la méthode VISÉE."""
    lot = client.post("/v1beta/properties/424242001:batchRunReports", json={})
    meta = client.get("/v1beta/properties/424242001/metadata")
    assert lot.json()["error"]["details"][0]["metadata"]["method"].endswith("BatchRunReports")
    assert meta.json()["error"]["details"][0]["metadata"]["method"].endswith("GetMetadata")


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

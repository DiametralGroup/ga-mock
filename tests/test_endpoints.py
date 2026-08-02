"""Surface HTTP — version jalon A1, étoffée aux jalons suivants."""


def test_health_public(client):
    reponse = client.get("/health")
    assert reponse.status_code == 200
    assert reponse.json() == {"status": "ok", "service": "ga-mock"}


CORPS_MINIMAL = {
    "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
    "metrics": [{"name": "sessions"}],
}


def test_pattern_deux_points_capture_la_propriete(client, bearer):
    """Le littéral `:runReport` après {property_id} doit router — et le
    paramètre doit contenir l'identifiant SANS le suffixe (la preuve : le
    contrôle de propriété accepte le bon id et refuse un autre)."""
    ok = client.post("/v1beta/properties/424242001:runReport", headers=bearer, json=CORPS_MINIMAL)
    assert ok.status_code == 200
    autre = client.post(
        "/v1beta/properties/999999999:runReport", headers=bearer, json=CORPS_MINIMAL
    )
    assert autre.status_code == 403
    assert autre.json()["error"]["status"] == "PERMISSION_DENIED"


def test_propriete_non_numerique(client, bearer):
    reponse = client.post("/v1beta/properties/abc:runReport", headers=bearer, json=CORPS_MINIMAL)
    assert reponse.status_code == 400
    assert reponse.json()["error"]["status"] == "INVALID_ARGUMENT"


def test_metadata_derivee_du_registre(client, bearer):
    reponse = client.get("/v1beta/properties/424242001/metadata", headers=bearer)
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["name"] == "properties/424242001/metadata"
    dimensions = {d["apiName"] for d in corps["dimensions"]}
    metriques = {m["apiName"]: m for m in corps["metrics"]}
    assert "date" in dimensions and "pagePath" in dimensions
    assert metriques["sessions"]["type"] == "TYPE_INTEGER"
    assert metriques["engagementRate"]["type"] == "TYPE_FLOAT"
    assert metriques["averageSessionDuration"]["type"] == "TYPE_SECONDS"
    assert all(not d["customDefinition"] for d in corps["dimensions"])


def test_metadata_propriete_zero_admise(client, bearer):
    reponse = client.get("/v1beta/properties/0/metadata", headers=bearer)
    assert reponse.status_code == 200
    assert reponse.json()["name"] == "properties/0/metadata"


def test_metadata_autre_propriete_refusee(client, bearer):
    reponse = client.get("/v1beta/properties/123456/metadata", headers=bearer)
    assert reponse.status_code == 403


def test_route_inconnue_enveloppe_google(client):
    reponse = client.get("/v1beta/does-not-exist")
    assert reponse.status_code == 404
    corps = reponse.json()
    assert "detail" not in corps
    assert corps["error"]["status"] == "NOT_FOUND"
    assert corps["error"]["code"] == 404


def test_mauvais_verbe_enveloppe_google(client):
    reponse = client.get("/v1beta/properties/424242001:runReport")
    assert reponse.status_code == 405
    assert reponse.json()["error"]["status"] == "METHOD_NOT_ALLOWED"

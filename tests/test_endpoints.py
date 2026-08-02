"""Surface HTTP — version jalon A1, étoffée aux jalons suivants."""


def test_health_public(client):
    reponse = client.get("/health")
    assert reponse.status_code == 200
    assert reponse.json() == {"status": "ok", "service": "ga-mock"}


def test_pattern_deux_points_capture_la_propriete(client):
    """Le littéral `:runReport` après {property_id} doit router — et le
    paramètre doit contenir l'identifiant SANS le suffixe."""
    reponse = client.post("/v1beta/properties/424242001:runReport")
    assert reponse.status_code == 501
    assert "properties/424242001" in reponse.json()["error"]["message"]


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

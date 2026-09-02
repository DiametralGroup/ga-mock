"""Surface HTTP — version jalon A1, étoffée aux jalons suivants."""

import pytest


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
    # `keyEvents` en TYPE_FLOAT : c'était une hypothèse, c'est désormais relevé
    # sur une vraie propriété.
    assert metriques["keyEvents"]["type"] == "TYPE_FLOAT"
    # proto3 : `customDefinition` false est ABSENT, pas rendu à false. Le
    # service ne l'écrit jamais pour les champs standard.
    assert all("customDefinition" not in d for d in corps["dimensions"])
    assert all("customDefinition" not in m for m in corps["metrics"])
    # Les anciens noms encore annoncés par le service, à l'identique.
    dimensions = {d["apiName"]: d for d in corps["dimensions"]}
    assert dimensions["dayOfWeek"]["deprecatedApiNames"] == ["dayOfWeekZero"]
    assert dimensions["sessionDefaultChannelGroup"]["deprecatedApiNames"] == [
        "sessionDefaultChannelGrouping"
    ]
    assert metriques["keyEvents"]["deprecatedApiNames"] == ["conversions"]
    assert metriques["sessionKeyEventRate"]["deprecatedApiNames"] == ["sessionConversionRate"]
    assert "deprecatedApiNames" not in metriques["sessions"]
    # Catégories et noms d'interface relevés eux aussi sur le service.
    assert dimensions["sessionSource"]["category"] == "Traffic Source"
    assert dimensions["newVsReturning"]["category"] == "User"
    assert metriques["userEngagementDuration"]["category"] == "User"
    assert metriques["sessionKeyEventRate"]["category"] == "Session"
    assert metriques["sessionsPerUser"]["uiName"] == "Sessions per active user"


def test_metadata_propriete_zero_admise(client, bearer):
    reponse = client.get("/v1beta/properties/0/metadata", headers=bearer)
    assert reponse.status_code == 200
    assert reponse.json()["name"] == "properties/0/metadata"


def test_metadata_autre_propriete_refusee(client, bearer):
    reponse = client.get("/v1beta/properties/123456/metadata", headers=bearer)
    assert reponse.status_code == 403


def test_route_inconnue_rend_du_html_pas_du_json(client):
    """Le ROUTAGE ne parle pas google.rpc.

    Relevé sur analyticsdata.googleapis.com le 2026-09-02 : un chemin inconnu
    rend une page HTML 404 de la passerelle. Le mock l'imite pour que le
    consommateur découvre ICI que `reponse.json()` peut échouer — et qu'il n'y
    a donc pas UNE seule forme d'erreur sur cette API.
    """
    reponse = client.get("/v1beta/does-not-exist")
    assert reponse.status_code == 404
    assert reponse.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in reponse.text
    with pytest.raises(ValueError):
        reponse.json()


def test_verbe_inconnu_rend_du_html_pas_du_json(client):
    reponse = client.post("/v1beta/properties/424242001:runNothing", json={})
    assert reponse.status_code == 404
    assert reponse.headers["content-type"].startswith("text/html")


def test_mauvais_verbe_est_un_404_pas_un_405(client):
    """Le vendeur ne rend JAMAIS 405 : la méthode HTTP fait partie du motif de
    route, un mauvais verbe est donc une route qui n'existe pas. `GET` sur
    `:runReport` rend bien 404 + HTML sur le vrai service."""
    reponse = client.get("/v1beta/properties/424242001:runReport")
    assert reponse.status_code == 404
    assert reponse.headers["content-type"].startswith("text/html")


def test_les_affordances_du_mock_gardent_une_erreur_lisible(client):
    """La page HTML de la passerelle est réservée aux chemins du VENDEUR : une
    faute de frappe sur /__admin ou /health doit parler au développeur."""
    for chemin in ("/__admin/stat", "/health/typo"):
        reponse = client.get(chemin)
        assert reponse.status_code == 404
        assert reponse.headers["content-type"].startswith("application/json")
        assert "unknown mock path" in reponse.json()["error"]


def test_metadata_porte_les_comparaisons_standard(client, bearer):
    """GA4 livre neuf comparaisons d'office sur toute propriété — mais AUCUNE
    sur `properties/0`, qui ne décrit que le schéma commun."""
    propriete = client.get("/v1beta/properties/424242001/metadata", headers=bearer).json()
    zero = client.get("/v1beta/properties/0/metadata", headers=bearer).json()
    noms = [c["apiName"] for c in propriete["comparisons"]]
    assert noms[0] == "comparisons/allUsers"
    assert len(noms) == 9
    assert all(n.startswith("comparisons/") for n in noms)
    assert "comparisons" not in zero


def test_champ_json_inconnu_refuse_avec_ses_violations(client, bearer):
    """La couche de transcodage refuse une clé inconnue AVANT la méthode :
    `dateRange` au lieu de `dateRanges` doit casser ici, pas passer en silence.
    Toutes les clés fautives sont listées, une violation chacune."""
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "metrics": [{"name": "sessions"}],
            "pasUnChamp": 1,
            "nonPlus": 2,
        },
    )
    assert reponse.status_code == 400
    erreur = reponse.json()["error"]
    assert erreur["status"] == "INVALID_ARGUMENT"
    assert erreur["message"] == (
        'Invalid JSON payload received. Unknown name "pasUnChamp": Cannot find field.\n'
        'Invalid JSON payload received. Unknown name "nonPlus": Cannot find field.'
    )
    violations = erreur["details"][0]["fieldViolations"]
    assert erreur["details"][0]["@type"] == "type.googleapis.com/google.rpc.BadRequest"
    assert len(violations) == 2
    # Pas de `field` ici : la couche ne sait pas nommer un champ qui n'existe pas.
    assert all(set(v) == {"description"} for v in violations)


def test_champ_inconnu_imbrique_nomme_le_chemin_proto(client, bearer):
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02", "zzz": 1}],
            "metrics": [{"name": "sessions"}],
        },
    )
    assert reponse.status_code == 400
    # snake_case : c'est le nom du champ PROTO, pas la clé JSON envoyée.
    assert reponse.json()["error"]["message"] == (
        "Invalid JSON payload received. Unknown name \"zzz\" at 'date_ranges[0]': "
        "Cannot find field."
    )


def test_agregation_hors_enumeration_nomme_le_champ(client, bearer):
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "metrics": [{"name": "sessions"}],
            "metricAggregations": ["MOYENNE"],
        },
    )
    assert reponse.status_code == 400
    erreur = reponse.json()["error"]
    attendu = (
        "Invalid value at 'metric_aggregations[0]' "
        '(type.googleapis.com/google.analytics.data.v1beta.MetricAggregation), "MOYENNE"'
    )
    assert erreur["message"] == attendu
    violation = erreur["details"][0]["fieldViolations"][0]
    assert violation == {"field": "metric_aggregations[0]", "description": attendu}


def test_count_est_dans_l_enumeration_mais_refuse(client, bearer):
    """`COUNT` est une valeur légale du proto — le service la refuse quand même,
    et avec un message de MÉTHODE (sans `details`), pas de transcodage."""
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "metrics": [{"name": "sessions"}],
            "metricAggregations": ["COUNT"],
        },
    )
    assert reponse.status_code == 400
    erreur = reponse.json()["error"]
    assert erreur["message"] == "Metric aggregation Count is not supported in ReportRequest."
    assert "details" not in erreur


def test_bornes_de_dates_du_service(client, bearer):
    """`start_date` doit être STRICTEMENT postérieure au 2015-08-13 : le
    2015-08-13 lui-même est refusé, le 2015-08-14 passe."""

    def statut(debut):
        return client.post(
            "/v1beta/properties/424242001:runReport",
            headers=bearer,
            json={
                "dateRanges": [{"startDate": debut, "endDate": "2015-09-01"}],
                "metrics": [{"name": "sessions"}],
            },
        )

    refuse = statut("2015-08-13")
    assert refuse.status_code == 400
    assert refuse.json()["error"]["message"] == (
        "start_date = 2015-08-13 must be greater than 2015-08-13 and less than 3000-01-01."
    )
    assert statut("2015-08-14").status_code == 200


def test_corps_vide_est_un_message_vide_pas_une_erreur_de_parsing(client, bearer):
    """Relevé : un corps VIDE n'est pas rejeté par le parseur, il traverse en
    message vide et échoue en validation. Personne ne devine ça."""
    reponse = client.post("/v1beta/properties/424242001:runReport", headers=bearer, content=b"")
    assert reponse.status_code == 400
    assert reponse.json()["error"]["message"] == "A dateRange is required."


def test_racine_non_objet_a_son_propre_message(client, bearer):
    for charge in (b"null", b"[]"):
        reponse = client.post(
            "/v1beta/properties/424242001:runReport", headers=bearer, content=charge
        )
        assert reponse.status_code == 400
        assert reponse.json()["error"]["message"] == (
            'Invalid JSON payload received. Unknown name "": Root element must be a message.'
        )


def test_json_casse_rend_un_message_multiligne_avec_caret(client, bearer):
    """Trois lignes : la raison, la ligne fautive, un caret sous la colonne.
    Le LIBELLÉ de la raison vient du parseur d'ici (approximation consignée),
    la géométrie est celle du vendeur."""
    reponse = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        content=b"{ceci n'est pas du json",
    )
    assert reponse.status_code == 400
    lignes = reponse.json()["error"]["message"].split("\n")
    assert len(lignes) == 3
    assert lignes[0].startswith("Invalid JSON payload received. ")
    assert lignes[1] == "{ceci n'est pas du json"
    assert lignes[2].endswith("^") and set(lignes[2][:-1]) <= {" "}


def test_ordre_des_controles_du_service(client, bearer):
    """L'ordre relevé sur le service, cas doublement fautifs à l'appui : la
    validité d'un NOM et la positivité de `limit` priment sur l'exigence d'une
    plage, mais les bornes de cardinalité passent APRÈS elle."""
    chemin = "/v1beta/properties/424242001:runReport"
    dix_dims = [
        {"name": n}
        for n in (
            "date",
            "week",
            "month",
            "yearMonth",
            "dayOfWeek",
            "country",
            "region",
            "city",
            "browser",
            "deviceCategory",
        )
    ]

    def message(corps):
        reponse = client.post(chemin, headers=bearer, json=corps)
        assert reponse.status_code == 400, reponse.text
        return reponse.json()["error"]["message"]

    # nom invalide + pas de plage → c'est le NOM qui sort
    assert "is not a valid dimension." in message({"dimensions": [{"name": "pasUneDim"}]})
    # limit négative + pas de plage → c'est LIMIT qui sort
    assert message({"metrics": [{"name": "sessions"}], "limit": -1}) == (
        "limit must be positive. The API received limit = -1"
    )
    # dix dimensions + pas de plage → c'est la PLAGE qui sort
    assert message({"dimensions": dix_dims}) == "A dateRange is required."
    # champ JSON inconnu → le transcodage passe avant tout le reste
    assert message({"zzz": 1}).startswith('Invalid JSON payload received. Unknown name "zzz"')

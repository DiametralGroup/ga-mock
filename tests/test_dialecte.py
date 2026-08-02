"""Le dialecte proto3-JSON de la surface v1beta : formes, omissions, bornes.

Ces tests figent ce que les consommateurs vont apprendre à leurs dépens si le
mock ment : int64 en chaînes, champs répétés vides ABSENTS, valeurs de
métriques toujours en chaînes, plafonds silencieux.
"""

from datetime import date, timedelta

from ga_mock.dataset import build_day

SEED = 42
CHEMIN = "/v1beta/properties/424242001:runReport"
CHEMIN_BATCH = "/v1beta/properties/424242001:batchRunReports"


def _rapport(client, bearer, corps):
    reponse = client.post(CHEMIN, headers=bearer, json=corps)
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _corps(**surcharges):
    corps = {
        "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-07"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
    }
    corps.update(surcharges)
    return corps


def test_limit_chaine_et_nombre_equivalents(client, bearer):
    en_nombre = _rapport(client, bearer, _corps(limit=3))
    en_chaine = _rapport(client, bearer, _corps(limit="3"))
    assert en_nombre["rows"] == en_chaine["rows"]
    assert len(en_nombre["rows"]) == 3


def test_valeurs_metriques_toujours_en_chaines(client, bearer):
    rapport = _rapport(
        client,
        bearer,
        _corps(metrics=[{"name": "sessions"}, {"name": "engagementRate"}]),
    )
    for ligne in rapport["rows"]:
        for valeur in ligne["metricValues"]:
            assert isinstance(valeur["value"], str)
    assert isinstance(rapport["rowCount"], int)


def test_rapport_vide_omet_rows_et_rowcount(client, bearer):
    """proto3-JSON : un champ répété vide ou un int32 à zéro n'apparaissent
    PAS. Le client qui fait `body["rows"]` doit casser ICI, pas en prod."""
    rapport = _rapport(
        client,
        bearer,
        _corps(dateRanges=[{"startDate": "2024-03-01", "endDate": "2024-03-05"}]),
    )
    assert "rows" not in rapport
    assert "rowCount" not in rapport
    assert rapport["metricHeaders"] == [{"name": "sessions", "type": "TYPE_INTEGER"}]
    assert rapport["dimensionHeaders"] == [{"name": "date"}]
    assert rapport["metadata"] == {"currencyCode": "EUR", "timeZone": "Europe/Paris"}
    assert rapport["kind"] == "analyticsData#runReport"


def test_dates_relatives_resolues_sur_l_horloge_virtuelle(client, bearer):
    aujourdhui = _rapport(
        client, bearer, _corps(dateRanges=[{"startDate": "today", "endDate": "today"}])
    )
    for ligne in aujourdhui.get("rows", []):
        assert ligne["dimensionValues"][0]["value"] == "20260715"

    hier = _rapport(
        client,
        bearer,
        _corps(dateRanges=[{"startDate": "yesterday", "endDate": "yesterday"}]),
    )
    for ligne in hier["rows"]:
        assert ligne["dimensionValues"][0]["value"] == "20260714"

    semaine = _rapport(
        client,
        bearer,
        _corps(dateRanges=[{"startDate": "7daysAgo", "endDate": "today"}]),
    )
    dates = {ligne["dimensionValues"][0]["value"] for ligne in semaine["rows"]}
    assert min(dates) == "20260708"
    assert max(dates) <= "20260715"


def test_multi_plages_ajoutent_la_dimension_dateRange(client, bearer):
    rapport = _rapport(
        client,
        bearer,
        _corps(
            dateRanges=[
                {"startDate": "2026-06-01", "endDate": "2026-06-03"},
                {"startDate": "2026-05-01", "endDate": "2026-05-03", "name": "comparaison"},
            ]
        ),
    )
    assert rapport["dimensionHeaders"] == [{"name": "date"}, {"name": "dateRange"}]
    labels = {ligne["dimensionValues"][1]["value"] for ligne in rapport["rows"]}
    assert labels == {"date_range_0", "comparaison"}


def test_agregations_reserved_et_totaux_exacts(client, bearer):
    debut, fin = date(2026, 6, 1), date(2026, 6, 7)
    rapport = _rapport(
        client,
        bearer,
        _corps(
            dateRanges=[{"startDate": str(debut), "endDate": str(fin)}],
            metrics=[{"name": "sessions"}, {"name": "totalUsers"}],
            metricAggregations=["TOTAL", "MAXIMUM", "MINIMUM"],
        ),
    )
    total = rapport["totals"][0]
    assert total["dimensionValues"][0]["value"] == "RESERVED_TOTAL"
    users_attendus = set()
    jours = []
    d = debut
    while d <= fin:
        sessions = build_day(SEED, d)
        users_attendus.update(s.user_id for s in sessions)
        jours.append(len(sessions))
        d += timedelta(days=1)
    # TOTAL de totalUsers = dédoublonnage GLOBAL, pas la somme des lignes
    assert int(total["metricValues"][1]["value"]) == len(users_attendus)
    assert int(total["metricValues"][0]["value"]) == sum(jours)
    maximum = rapport["maximums"][0]
    assert maximum["dimensionValues"][0]["value"] == "RESERVED_MAX"
    assert int(maximum["metricValues"][0]["value"]) == max(jours)
    minimum = rapport["minimums"][0]
    assert minimum["dimensionValues"][0]["value"] == "RESERVED_MIN"
    assert int(minimum["metricValues"][0]["value"]) == min(jours)


def test_metrique_inconnue(client, bearer):
    reponse = client.post(CHEMIN, headers=bearer, json=_corps(metrics=[{"name": "pasUneMetrique"}]))
    assert reponse.status_code == 400
    assert reponse.json()["error"]["message"] == "Field pasUneMetrique is not a valid metric."


def test_bornes_dimensions_et_metriques(client, bearer):
    dix_dims = [
        {"name": n}
        for n in (
            "date",
            "week",
            "month",
            "yearMonth",
            "dayOfWeek",
            "sessionSource",
            "sessionMedium",
            "deviceCategory",
            "browser",
            "country",
        )
    ]
    reponse = client.post(CHEMIN, headers=bearer, json=_corps(dimensions=dix_dims))
    assert reponse.status_code == 400
    assert "9 dimensions" in reponse.json()["error"]["message"]

    onze_metriques = [
        {"name": n}
        for n in (
            "sessions",
            "totalUsers",
            "activeUsers",
            "newUsers",
            "engagedSessions",
            "engagementRate",
            "bounceRate",
            "averageSessionDuration",
            "userEngagementDuration",
            "screenPageViews",
            "eventCount",
        )
    ]
    reponse = client.post(CHEMIN, headers=bearer, json=_corps(metrics=onze_metriques))
    assert reponse.status_code == 400
    assert "10 metrics" in reponse.json()["error"]["message"]


def test_limit_excessive_plafonnee_en_silence(client, bearer):
    rapport = _rapport(client, bearer, _corps(limit=300_000))
    assert rapport["rowCount"] == 7


def test_keep_empty_rows_spine_calendaire(client, bearer):
    """Avec un filtre qui ne matche rien, keepEmptyRows doit quand même rendre
    la colonne vertébrale calendaire — le cas BI « série sans trous »."""
    rapport = _rapport(
        client,
        bearer,
        _corps(
            dateRanges=[{"startDate": "2026-06-01", "endDate": "2026-06-05"}],
            keepEmptyRows=True,
            dimensionFilter={
                "filter": {
                    "fieldName": "date",
                    "stringFilter": {"matchType": "EXACT", "value": "19700101"},
                }
            },
        ),
    )
    assert rapport["rowCount"] == 5
    assert all(ligne["metricValues"][0]["value"] == "0" for ligne in rapport["rows"])


def test_property_quota(client, bearer):
    premier = _rapport(client, bearer, _corps(returnPropertyQuota=True))
    quota1 = premier["propertyQuota"]
    assert set(quota1) == {
        "tokensPerDay",
        "tokensPerHour",
        "concurrentRequests",
        "serverErrorsPerProjectPerHour",
        "potentiallyThresholdedRequestsPerHour",
    }
    second = _rapport(client, bearer, _corps(returnPropertyQuota=True))
    quota2 = second["propertyQuota"]
    assert quota1["tokensPerDay"]["remaining"] - quota2["tokensPerDay"]["remaining"] == 10


def test_cohortes_refusees_explicitement(client, bearer):
    reponse = client.post(CHEMIN, headers=bearer, json=_corps(cohortSpec={}))
    assert reponse.status_code == 400
    assert "not supported" in reponse.json()["error"]["message"]


def test_ordre_par_defaut_deterministe(client, bearer):
    corps = _corps(
        dimensions=[{"name": "sessionSource"}],
        metrics=[{"name": "sessions"}, {"name": "totalUsers"}],
    )
    premier = _rapport(client, bearer, corps)
    second = _rapport(client, bearer, corps)
    assert premier["rows"] == second["rows"]
    valeurs = [int(ligne["metricValues"][0]["value"]) for ligne in premier["rows"]]
    assert valeurs == sorted(valeurs, reverse=True)


def test_orderbys_dimension_et_metrique(client, bearer):
    descendant = _rapport(
        client,
        bearer,
        _corps(orderBys=[{"desc": True, "dimension": {"dimensionName": "date"}}]),
    )
    dates = [ligne["dimensionValues"][0]["value"] for ligne in descendant["rows"]]
    assert dates == sorted(dates, reverse=True)

    croissant = _rapport(
        client,
        bearer,
        _corps(orderBys=[{"metric": {"metricName": "sessions"}}]),
    )
    valeurs = [int(ligne["metricValues"][0]["value"]) for ligne in croissant["rows"]]
    assert valeurs == sorted(valeurs)


def test_orderby_hors_rapport_refuse(client, bearer):
    reponse = client.post(
        CHEMIN,
        headers=bearer,
        json=_corps(orderBys=[{"dimension": {"dimensionName": "country"}}]),
    )
    assert reponse.status_code == 400


def test_batch_run_reports(client, bearer):
    reponse = client.post(
        CHEMIN_BATCH,
        headers=bearer,
        json={"requests": [_corps(), _corps(metrics=[{"name": "totalUsers"}])]},
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["kind"] == "analyticsData#batchRunReports"
    assert len(corps["reports"]) == 2
    assert corps["reports"][0]["kind"] == "analyticsData#runReport"

    trop = client.post(
        CHEMIN_BATCH, headers=bearer, json={"requests": [_corps() for _ in range(6)]}
    )
    assert trop.status_code == 400
    assert "5 requests" in trop.json()["error"]["message"]


def test_plage_inversee_et_date_invalide(client, bearer):
    inversee = client.post(
        CHEMIN,
        headers=bearer,
        json=_corps(dateRanges=[{"startDate": "2026-06-07", "endDate": "2026-06-01"}]),
    )
    assert inversee.status_code == 400

    invalide = client.post(
        CHEMIN,
        headers=bearer,
        json=_corps(dateRanges=[{"startDate": "01/06/2026", "endDate": "2026-06-07"}]),
    )
    assert invalide.status_code == 400
    assert "Invalid date" in invalide.json()["error"]["message"]

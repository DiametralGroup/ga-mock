"""Exactitude des agrégats : recomptes brute-force contre `build_day`.

Les fenêtres de test sont dans le passé (l'ancre virtuelle est au 15 juillet
2026) : tous les jours interrogés sont sortis de la fenêtre de fraîcheur, les
sessions visibles sont donc EXACTEMENT celles de `build_day`.
"""

from datetime import date, timedelta

from ga_mock.dataset import build_day

SEED = 42
PROPRIETE = "/v1beta/properties/424242001:runReport"


def _rapport(client, bearer, corps):
    reponse = client.post(PROPRIETE, headers=bearer, json=corps)
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _jours(debut, fin):
    d = debut
    while d <= fin:
        yield d
        d += timedelta(days=1)


def test_sessions_par_date_egales_au_recompte_brut(client, bearer):
    corps = {
        "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-07"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
    }
    rapport = _rapport(client, bearer, corps)
    obtenu = {
        ligne["dimensionValues"][0]["value"]: int(ligne["metricValues"][0]["value"])
        for ligne in rapport["rows"]
    }
    attendu = {
        f"{d:%Y%m%d}": len(build_day(SEED, d)) for d in _jours(date(2026, 6, 1), date(2026, 6, 7))
    }
    assert obtenu == attendu
    assert rapport["rowCount"] == 7


def test_totalusers_dedoublonne_pour_de_vrai(client, bearer):
    """LE test qui justifie le dataset au niveau session : la somme des
    totalUsers quotidiens SUR-compte (visiteurs récurrents), la valeur globale
    doit être le dédoublonnage exact."""
    debut, fin = date(2026, 5, 1), date(2026, 5, 28)
    plage = [{"startDate": str(debut), "endDate": str(fin)}]
    global_ = _rapport(client, bearer, {"dateRanges": plage, "metrics": [{"name": "totalUsers"}]})
    valeur_globale = int(global_["rows"][0]["metricValues"][0]["value"])
    attendu = len({s.user_id for d in _jours(debut, fin) for s in build_day(SEED, d)})
    assert valeur_globale == attendu

    par_jour = _rapport(
        client,
        bearer,
        {
            "dateRanges": plage,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "totalUsers"}],
        },
    )
    somme_quotidienne = sum(int(ligne["metricValues"][0]["value"]) for ligne in par_jour["rows"])
    assert somme_quotidienne > valeur_globale


def test_pages_vues_et_nouveaux(client, bearer):
    debut, fin = date(2026, 6, 1), date(2026, 6, 7)
    rapport = _rapport(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": str(debut), "endDate": str(fin)}],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "newUsers"},
                {"name": "engagedSessions"},
            ],
        },
    )
    valeurs = [int(v["value"]) for v in rapport["rows"][0]["metricValues"]]
    sessions = [s for d in _jours(debut, fin) for s in build_day(SEED, d)]
    assert valeurs[0] == sum(len(s.pages) for s in sessions)
    assert valeurs[1] == sum(1 for s in sessions if s.is_new)
    assert valeurs[2] == sum(1 for s in sessions if s.engaged)


def test_taux_engagement_et_rebond_complementaires(client, bearer):
    rapport = _rapport(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-07"}],
            "metrics": [{"name": "engagementRate"}, {"name": "bounceRate"}],
        },
    )
    engagement, rebond = (float(v["value"]) for v in rapport["rows"][0]["metricValues"])
    assert 0.4 < engagement < 0.95
    assert abs(engagement + rebond - 1.0) < 1e-9


def test_pagination_disjointe_et_rowcount_stable(client, bearer):
    corps = {
        "dateRanges": [{"startDate": "2026-05-01", "endDate": "2026-05-30"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
        "limit": 10,
    }
    page1 = _rapport(client, bearer, corps)
    page2 = _rapport(client, bearer, {**corps, "offset": 10})
    assert page1["rowCount"] == page2["rowCount"] == 30
    assert len(page1["rows"]) == len(page2["rows"]) == 10
    dates1 = {ligne["dimensionValues"][0]["value"] for ligne in page1["rows"]}
    dates2 = {ligne["dimensionValues"][0]["value"] for ligne in page2["rows"]}
    assert not dates1 & dates2


def test_fanout_pagepath_sessions_exactes(client, bearer):
    """Sous fan-out, `sessions` par page reste un dénombrement DISTINCT : la
    somme des lignes dépasse le total (une session visite plusieurs pages),
    mais chaque ligne reste bornée par le total — comme le vrai GA."""
    debut, fin = date(2026, 6, 1), date(2026, 6, 3)
    plage = [{"startDate": str(debut), "endDate": str(fin)}]
    par_page = _rapport(
        client,
        bearer,
        {
            "dateRanges": plage,
            "dimensions": [{"name": "pagePath"}],
            "metrics": [{"name": "sessions"}, {"name": "screenPageViews"}],
        },
    )
    total = len([s for d in _jours(debut, fin) for s in build_day(SEED, d)])
    somme_sessions = 0
    for ligne in par_page["rows"]:
        sessions_page = int(ligne["metricValues"][0]["value"])
        assert sessions_page <= total
        somme_sessions += sessions_page
    assert somme_sessions > total

    # les vues par page recomptées brute-force
    vues_attendues: dict[str, int] = {}
    for d in _jours(debut, fin):
        for s in build_day(SEED, d):
            for p in s.pages:
                vues_attendues[p] = vues_attendues.get(p, 0) + 1
    vues_obtenues = {
        ligne["dimensionValues"][0]["value"]: int(ligne["metricValues"][1]["value"])
        for ligne in par_page["rows"]
    }
    assert vues_obtenues == vues_attendues


def test_dimension_inconnue_erreur_google(client, bearer):
    reponse = client.post(
        PROPRIETE,
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "dimensions": [{"name": "pasUneDimension"}],
            "metrics": [{"name": "sessions"}],
        },
    )
    assert reponse.status_code == 400
    corps = reponse.json()
    assert corps["error"]["status"] == "INVALID_ARGUMENT"
    assert corps["error"]["message"] == "Field pasUneDimension is not a valid dimension."


def test_sans_metrique_refuse(client, bearer):
    reponse = client.post(
        PROPRIETE,
        headers=bearer,
        json={"dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}]},
    )
    assert reponse.status_code == 400
    assert "at least one metric" in reponse.json()["error"]["message"]


def test_filtre_exact_sur_canal(client, bearer):
    debut, fin = date(2026, 6, 1), date(2026, 6, 5)
    rapport = _rapport(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": str(debut), "endDate": str(fin)}],
            "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "stringFilter": {"matchType": "EXACT", "value": "direct"},
                }
            },
        },
    )
    # caseSensitive vaut false par défaut : « direct » matche « Direct »
    assert len(rapport["rows"]) == 1
    assert rapport["rows"][0]["dimensionValues"][0]["value"] == "Direct"
    attendu = sum(
        1 for d in _jours(debut, fin) for s in build_day(SEED, d) if s.channel_group == "Direct"
    )
    assert int(rapport["rows"][0]["metricValues"][0]["value"]) == attendu


def test_full_regexp_contre_partial_regexp(client, bearer):
    plage = [{"startDate": "2026-06-01", "endDate": "2026-06-03"}]

    def compte(match_type):
        rapport = _rapport(
            client,
            bearer,
            {
                "dateRanges": plage,
                "dimensions": [{"name": "pagePath"}],
                "metrics": [{"name": "screenPageViews"}],
                "dimensionFilter": {
                    "filter": {
                        "fieldName": "pagePath",
                        "stringFilter": {"matchType": match_type, "value": "/blog"},
                    }
                },
            },
        )
        return rapport.get("rows", [])

    # FULL_REGEXP : « /blog » ne matche que la page d'index, pas les articles
    complets = compte("FULL_REGEXP")
    assert {ligne["dimensionValues"][0]["value"] for ligne in complets} == {"/blog"}
    # PARTIAL_REGEXP : toutes les pages contenant /blog
    partiels = compte("PARTIAL_REGEXP")
    assert len(partiels) > 1
    assert all("/blog" in ligne["dimensionValues"][0]["value"] for ligne in partiels)


def test_inlist_et_notexpression_combines(client, bearer):
    debut, fin = date(2026, 6, 1), date(2026, 6, 5)
    rapport = _rapport(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": str(debut), "endDate": str(fin)}],
            "dimensions": [{"name": "deviceCategory"}, {"name": "country"}],
            "metrics": [{"name": "sessions"}],
            "dimensionFilter": {
                "andGroup": {
                    "expressions": [
                        {
                            "filter": {
                                "fieldName": "deviceCategory",
                                "inListFilter": {"values": ["desktop", "tablet"]},
                            }
                        },
                        {
                            "notExpression": {
                                "filter": {
                                    "fieldName": "country",
                                    "stringFilter": {
                                        "matchType": "EXACT",
                                        "value": "France",
                                    },
                                }
                            }
                        },
                    ]
                }
            },
        },
    )
    attendu = sum(
        1
        for d in _jours(debut, fin)
        for s in build_day(SEED, d)
        if s.device_category in ("desktop", "tablet") and s.country != "France"
    )
    obtenu = sum(int(ligne["metricValues"][0]["value"]) for ligne in rapport["rows"])
    assert obtenu == attendu
    assert all(ligne["dimensionValues"][1]["value"] != "France" for ligne in rapport["rows"])


def test_metricfilter_est_un_having(client, bearer):
    plage = [{"startDate": "2026-05-01", "endDate": "2026-05-30"}]
    sans_filtre = _rapport(
        client,
        bearer,
        {
            "dateRanges": plage,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
        },
    )
    valeurs = sorted(
        (int(ligne["metricValues"][0]["value"]) for ligne in sans_filtre["rows"]),
        reverse=True,
    )
    seuil = valeurs[len(valeurs) // 2]  # la médiane : coupe une partie des lignes
    filtre = _rapport(
        client,
        bearer,
        {
            "dateRanges": plage,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "metricFilter": {
                "filter": {
                    "fieldName": "sessions",
                    "numericFilter": {
                        "operation": "GREATER_THAN",
                        "value": {"int64Value": str(seuil)},
                    },
                }
            },
        },
    )
    assert filtre["rowCount"] == sum(1 for v in valeurs if v > seuil)
    assert all(int(ligne["metricValues"][0]["value"]) > seuil for ligne in filtre["rows"])


def test_betweenfilter_inclusif(client, bearer):
    plage = [{"startDate": "2026-05-01", "endDate": "2026-05-30"}]
    rapport = _rapport(
        client,
        bearer,
        {
            "dateRanges": plage,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "metricFilter": {
                "filter": {
                    "fieldName": "sessions",
                    "betweenFilter": {
                        "fromValue": {"int64Value": "20"},
                        "toValue": {"doubleValue": 100},
                    },
                }
            },
        },
    )
    for ligne in rapport["rows"]:
        assert 20 <= int(ligne["metricValues"][0]["value"]) <= 100


def test_filtre_sur_champ_non_demande_refuse(client, bearer):
    reponse = client.post(
        PROPRIETE,
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "country",
                    "stringFilter": {"matchType": "EXACT", "value": "France"},
                }
            },
        },
    )
    assert reponse.status_code == 400
    assert "must be a requested dimension" in reponse.json()["error"]["message"]

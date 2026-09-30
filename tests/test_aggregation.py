"""Aggregate correctness: brute-force recounts against `build_day`.

Test windows are in the past (the virtual anchor is July 15, 2026): every
day queried is out of the freshness window, so the sessions seen are
EXACTLY those from `build_day`.
"""

from datetime import date, timedelta

from ga_mock.dataset import build_day

SEED = 42
PROPERTY = "/v1beta/properties/424242001:runReport"


def _report(client, bearer, body):
    response = client.post(PROPERTY, headers=bearer, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def test_sessions_by_date_equal_the_brute_force_recount(client, bearer):
    body = {
        "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-07"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
    }
    report = _report(client, bearer, body)
    obtained = {
        row["dimensionValues"][0]["value"]: int(row["metricValues"][0]["value"])
        for row in report["rows"]
    }
    expected = {
        f"{d:%Y%m%d}": len(build_day(SEED, d)) for d in _days(date(2026, 6, 1), date(2026, 6, 7))
    }
    assert obtained == expected
    assert report["rowCount"] == 7


def test_totalusers_deduplicates_for_real(client, bearer):
    """THE test that justifies the session-level dataset: summing daily
    totalUsers OVER-counts (returning visitors), the global value must be the
    exact deduplication."""
    start, end = date(2026, 5, 1), date(2026, 5, 28)
    range_ = [{"startDate": str(start), "endDate": str(end)}]
    global_ = _report(client, bearer, {"dateRanges": range_, "metrics": [{"name": "totalUsers"}]})
    global_value = int(global_["rows"][0]["metricValues"][0]["value"])
    expected = len({s.user_id for d in _days(start, end) for s in build_day(SEED, d)})
    assert global_value == expected

    by_day = _report(
        client,
        bearer,
        {
            "dateRanges": range_,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "totalUsers"}],
        },
    )
    daily_sum = sum(int(row["metricValues"][0]["value"]) for row in by_day["rows"])
    assert daily_sum > global_value


def test_pageviews_and_new_users(client, bearer):
    start, end = date(2026, 6, 1), date(2026, 6, 7)
    report = _report(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "newUsers"},
                {"name": "engagedSessions"},
            ],
        },
    )
    values = [int(v["value"]) for v in report["rows"][0]["metricValues"]]
    sessions = [s for d in _days(start, end) for s in build_day(SEED, d)]
    assert values[0] == sum(len(s.pages) for s in sessions)
    assert values[1] == sum(1 for s in sessions if s.is_new)
    assert values[2] == sum(1 for s in sessions if s.engaged)


def test_engagement_rate_and_bounce_rate_are_complementary(client, bearer):
    report = _report(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-07"}],
            "metrics": [{"name": "engagementRate"}, {"name": "bounceRate"}],
        },
    )
    engagement, bounce = (float(v["value"]) for v in report["rows"][0]["metricValues"])
    assert 0.4 < engagement < 0.95
    assert abs(engagement + bounce - 1.0) < 1e-9


def test_disjoint_pagination_and_stable_rowcount(client, bearer):
    body = {
        "dateRanges": [{"startDate": "2026-05-01", "endDate": "2026-05-30"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
        "limit": 10,
    }
    page1 = _report(client, bearer, body)
    page2 = _report(client, bearer, {**body, "offset": 10})
    assert page1["rowCount"] == page2["rowCount"] == 30
    assert len(page1["rows"]) == len(page2["rows"]) == 10
    dates1 = {row["dimensionValues"][0]["value"] for row in page1["rows"]}
    dates2 = {row["dimensionValues"][0]["value"] for row in page2["rows"]}
    assert not dates1 & dates2


def test_pagepath_fanout_exact_sessions(client, bearer):
    """Under fan-out, `sessions` per page stays a DISTINCT count: the sum of
    rows exceeds the total (a session visits several pages), but each row
    stays bounded by the total — just like real GA."""
    start, end = date(2026, 6, 1), date(2026, 6, 3)
    range_ = [{"startDate": str(start), "endDate": str(end)}]
    by_page = _report(
        client,
        bearer,
        {
            "dateRanges": range_,
            "dimensions": [{"name": "pagePath"}],
            "metrics": [{"name": "sessions"}, {"name": "screenPageViews"}],
        },
    )
    total = len([s for d in _days(start, end) for s in build_day(SEED, d)])
    sessions_sum = 0
    for row in by_page["rows"]:
        page_sessions = int(row["metricValues"][0]["value"])
        assert page_sessions <= total
        sessions_sum += page_sessions
    assert sessions_sum > total

    # views per page recounted brute-force
    expected_views: dict[str, int] = {}
    for d in _days(start, end):
        for s in build_day(SEED, d):
            for p in s.pages:
                expected_views[p] = expected_views.get(p, 0) + 1
    obtained_views = {
        row["dimensionValues"][0]["value"]: int(row["metricValues"][1]["value"])
        for row in by_page["rows"]
    }
    assert obtained_views == expected_views


def test_unknown_dimension_google_error(client, bearer):
    response = client.post(
        PROPERTY,
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "dimensions": [{"name": "notADimension"}],
            "metrics": [{"name": "sessions"}],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["status"] == "INVALID_ARGUMENT"
    # Vendor wording, down to the spacing (ONE space after "dimension.",
    # TWO after "metric."), followed by the schema URL and a trailing space.
    assert body["error"]["message"] == (
        "Field notADimension is not a valid dimension. For a list of valid "
        "dimensions and metrics, see https://developers.google.com/analytics/"
        "devguides/reporting/data/v1/api-schema "
    )


def test_report_without_metric_is_valid(client, bearer):
    """« Requests require dimensions and/or metrics »: a report with BARE
    dimensions is legal. It comes out without `metricHeaders` and its rows
    without `metricValues` — the mock used to require a metric, wrongly."""
    report = _report(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-05"}],
            "dimensions": [{"name": "date"}],
        },
    )
    assert "metricHeaders" not in report
    assert report["rowCount"] == 5
    assert all("metricValues" not in row for row in report["rows"])
    assert [row["dimensionValues"][0]["value"] for row in report["rows"]] == [
        "20260601",
        "20260602",
        "20260603",
        "20260604",
        "20260605",
    ]


def test_neither_dimension_nor_metric_rejected(client, bearer):
    response = client.post(
        PROPERTY,
        headers=bearer,
        json={"dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "Requests require dimensions and/or metrics. Most requests include both."
    )


def test_exact_filter_on_channel(client, bearer):
    start, end = date(2026, 6, 1), date(2026, 6, 5)
    report = _report(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
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
    # caseSensitive defaults to false: "direct" matches "Direct"
    assert len(report["rows"]) == 1
    assert report["rows"][0]["dimensionValues"][0]["value"] == "Direct"
    expected = sum(
        1 for d in _days(start, end) for s in build_day(SEED, d) if s.channel_group == "Direct"
    )
    assert int(report["rows"][0]["metricValues"][0]["value"]) == expected


def test_full_regexp_versus_partial_regexp(client, bearer):
    range_ = [{"startDate": "2026-06-01", "endDate": "2026-06-03"}]

    def count(match_type):
        report = _report(
            client,
            bearer,
            {
                "dateRanges": range_,
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
        return report.get("rows", [])

    # FULL_REGEXP: "/blog" only matches the index page, not the articles
    full = count("FULL_REGEXP")
    assert {row["dimensionValues"][0]["value"] for row in full} == {"/blog"}
    # PARTIAL_REGEXP: every page containing /blog
    partial = count("PARTIAL_REGEXP")
    assert len(partial) > 1
    assert all("/blog" in row["dimensionValues"][0]["value"] for row in partial)


def test_inlist_and_notexpression_combined(client, bearer):
    start, end = date(2026, 6, 1), date(2026, 6, 5)
    report = _report(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
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
    expected = sum(
        1
        for d in _days(start, end)
        for s in build_day(SEED, d)
        if s.device_category in ("desktop", "tablet") and s.country != "France"
    )
    obtained = sum(int(row["metricValues"][0]["value"]) for row in report["rows"])
    assert obtained == expected
    assert all(row["dimensionValues"][1]["value"] != "France" for row in report["rows"])


def test_metricfilter_is_a_having(client, bearer):
    range_ = [{"startDate": "2026-05-01", "endDate": "2026-05-30"}]
    without_filter = _report(
        client,
        bearer,
        {
            "dateRanges": range_,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
        },
    )
    values = sorted(
        (int(row["metricValues"][0]["value"]) for row in without_filter["rows"]),
        reverse=True,
    )
    threshold = values[len(values) // 2]  # the median: cuts off some rows
    filtered = _report(
        client,
        bearer,
        {
            "dateRanges": range_,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "metricFilter": {
                "filter": {
                    "fieldName": "sessions",
                    "numericFilter": {
                        "operation": "GREATER_THAN",
                        "value": {"int64Value": str(threshold)},
                    },
                }
            },
        },
    )
    assert filtered["rowCount"] == sum(1 for v in values if v > threshold)
    assert all(int(row["metricValues"][0]["value"]) > threshold for row in filtered["rows"])


def test_betweenfilter_is_inclusive(client, bearer):
    range_ = [{"startDate": "2026-05-01", "endDate": "2026-05-30"}]
    report = _report(
        client,
        bearer,
        {
            "dateRanges": range_,
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
    for row in report["rows"]:
        assert 20 <= int(row["metricValues"][0]["value"]) <= 100


def test_filter_on_a_field_not_requested_is_valid(client, bearer):
    """The service does NOT require the filtered field to be in the report:
    filtering on `country` while grouping by `date` works. The mock used to
    reject it — a consumer would see a 400 in dev where prod answers 200."""
    range_ = [{"startDate": "2026-06-01", "endDate": "2026-06-07"}]

    def sessions(filter_=None):
        body = {
            "dateRanges": range_,
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
        }
        if filter_:
            body["dimensionFilter"] = filter_
        report = _report(client, bearer, body)
        rows = report.get("rows", [])
        return sum(int(row["metricValues"][0]["value"]) for row in rows)

    france = sessions(
        {
            "filter": {
                "fieldName": "country",
                "stringFilter": {"matchType": "EXACT", "value": "France"},
            }
        }
    )
    expected = sum(
        1
        for d in _days(date(2026, 6, 1), date(2026, 6, 7))
        for s in build_day(SEED, d)
        if s.country == "France"
    )
    assert france == expected
    assert 0 < france < sessions()


def test_having_on_a_metric_not_requested_is_valid(client, bearer):
    """Same rule on the `metricFilter` side: the having's metric doesn't need
    to appear in the report, and it doesn't come out in the rows."""
    report = _report(
        client,
        bearer,
        {
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-30"}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "metricFilter": {
                "filter": {
                    "fieldName": "totalUsers",
                    "numericFilter": {
                        "operation": "GREATER_THAN",
                        "value": {"int64Value": "40"},
                    },
                }
            },
        },
    )
    assert [m["name"] for m in report["metricHeaders"]] == ["sessions"]
    assert all(len(row["metricValues"]) == 1 for row in report["rows"])
    kept_days = {row["dimensionValues"][0]["value"] for row in report["rows"]}
    expected = {
        f"{d:%Y%m%d}"
        for d in _days(date(2026, 6, 1), date(2026, 6, 30))
        if len({s.user_id for s in build_day(SEED, d)}) > 40
    }
    assert kept_days == expected


def test_filter_on_a_nonexistent_field_rejected(client, bearer):
    response = client.post(
        PROPERTY,
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "dimensionFilter": {"filter": {"fieldName": "notADim", "stringFilter": {"value": "x"}}},
        },
    )
    assert response.status_code == 400
    assert "is not a valid dimension." in response.json()["error"]["message"]


def test_empty_filter_isolates_unset_values(client, bearer):
    """`emptyFilter` exists in the v1beta schema; rejecting it would put a 400
    in dev where prod answers 200. The mock's world does produce `(not set)`
    (the "Unassigned" channel), so the predicate has something to bite on."""
    range_ = [{"startDate": "2026-06-01", "endDate": "2026-06-30"}]

    def campaigns(filter_):
        report = _report(
            client,
            bearer,
            {
                "dateRanges": range_,
                "dimensions": [{"name": "sessionCampaignName"}],
                "metrics": [{"name": "sessions"}],
                "dimensionFilter": filter_,
            },
        )
        return {row["dimensionValues"][0]["value"] for row in report.get("rows", [])}

    leaf = {"filter": {"fieldName": "sessionCampaignName", "emptyFilter": {}}}
    empty = campaigns(leaf)
    populated = campaigns({"notExpression": leaf})
    assert empty == {"(not set)"}
    assert "(not set)" not in populated
    # `(direct)` and `(organic)` are REAL values, not absences.
    assert {"(direct)", "(organic)"} <= populated


def test_unknown_leaf_predicate_rejected(client, bearer):
    response = client.post(
        PROPERTY,
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}],
            "dimensionFilter": {"filter": {"fieldName": "date", "notAPredicate": {}}},
        },
    )
    assert response.status_code == 400
    assert "emptyFilter" in response.json()["error"]["message"]

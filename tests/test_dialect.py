"""The proto3-JSON dialect of the v1beta surface: shapes, omissions, bounds.

These tests pin down what consumers will learn the hard way if the mock
lies: int64 as strings, empty repeated fields ABSENT, metric values always
as strings, silent caps.
"""

from datetime import date, timedelta

from ga_mock.dataset import build_day

SEED = 42
PATH = "/v1beta/properties/424242001:runReport"
PATH_BATCH = "/v1beta/properties/424242001:batchRunReports"


def _report(client, bearer, body):
    response = client.post(PATH, headers=bearer, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _body(**overrides):
    body = {
        "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-07"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
    }
    body.update(overrides)
    return body


def test_string_and_number_limit_equivalent(client, bearer):
    as_number = _report(client, bearer, _body(limit=3))
    as_string = _report(client, bearer, _body(limit="3"))
    assert as_number["rows"] == as_string["rows"]
    assert len(as_number["rows"]) == 3


def test_metric_values_always_as_strings(client, bearer):
    report = _report(
        client,
        bearer,
        _body(metrics=[{"name": "sessions"}, {"name": "engagementRate"}]),
    )
    for row in report["rows"]:
        for value in row["metricValues"]:
            assert isinstance(value["value"], str)
    assert isinstance(report["rowCount"], int)


def test_empty_report_omits_rows_and_rowcount(client, bearer):
    """proto3-JSON: an empty repeated field or a zero int32 do NOT appear.
    The client that does `body["rows"]` must break HERE, not in prod."""
    report = _report(
        client,
        bearer,
        _body(dateRanges=[{"startDate": "2024-03-01", "endDate": "2024-03-05"}]),
    )
    assert "rows" not in report
    assert "rowCount" not in report
    assert report["metricHeaders"] == [{"name": "sessions", "type": "TYPE_INTEGER"}]
    assert report["dimensionHeaders"] == [{"name": "date"}]
    assert report["metadata"] == {"currencyCode": "EUR", "timeZone": "Europe/Paris"}
    assert report["kind"] == "analyticsData#runReport"


def test_relative_dates_resolved_against_the_virtual_clock(client, bearer):
    today = _report(client, bearer, _body(dateRanges=[{"startDate": "today", "endDate": "today"}]))
    for row in today.get("rows", []):
        assert row["dimensionValues"][0]["value"] == "20260715"

    yesterday = _report(
        client,
        bearer,
        _body(dateRanges=[{"startDate": "yesterday", "endDate": "yesterday"}]),
    )
    for row in yesterday["rows"]:
        assert row["dimensionValues"][0]["value"] == "20260714"

    week = _report(
        client,
        bearer,
        _body(dateRanges=[{"startDate": "7daysAgo", "endDate": "today"}]),
    )
    dates = {row["dimensionValues"][0]["value"] for row in week["rows"]}
    assert min(dates) == "20260708"
    assert max(dates) <= "20260715"


def test_multi_ranges_add_the_dateRange_dimension(client, bearer):
    report = _report(
        client,
        bearer,
        _body(
            dateRanges=[
                {"startDate": "2026-06-01", "endDate": "2026-06-03"},
                {"startDate": "2026-05-01", "endDate": "2026-05-03", "name": "comparison"},
            ]
        ),
    )
    assert report["dimensionHeaders"] == [{"name": "date"}, {"name": "dateRange"}]
    labels = {row["dimensionValues"][1]["value"] for row in report["rows"]}
    assert labels == {"date_range_0", "comparison"}


def test_reserved_aggregations_and_exact_totals(client, bearer):
    start, end = date(2026, 6, 1), date(2026, 6, 7)
    report = _report(
        client,
        bearer,
        _body(
            dateRanges=[{"startDate": str(start), "endDate": str(end)}],
            metrics=[{"name": "sessions"}, {"name": "totalUsers"}],
            metricAggregations=["TOTAL", "MAXIMUM", "MINIMUM"],
        ),
    )
    total = report["totals"][0]
    assert total["dimensionValues"][0]["value"] == "RESERVED_TOTAL"
    expected_users = set()
    days = []
    d = start
    while d <= end:
        sessions = build_day(SEED, d)
        expected_users.update(s.user_id for s in sessions)
        days.append(len(sessions))
        d += timedelta(days=1)
    # TOTAL of totalUsers = GLOBAL deduplication, not the sum of the rows
    assert int(total["metricValues"][1]["value"]) == len(expected_users)
    assert int(total["metricValues"][0]["value"]) == sum(days)
    maximum = report["maximums"][0]
    assert maximum["dimensionValues"][0]["value"] == "RESERVED_MAX"
    assert int(maximum["metricValues"][0]["value"]) == max(days)
    minimum = report["minimums"][0]
    assert minimum["dimensionValues"][0]["value"] == "RESERVED_MIN"
    assert int(minimum["metricValues"][0]["value"]) == min(days)


def test_unknown_metric(client, bearer):
    response = client.post(PATH, headers=bearer, json=_body(metrics=[{"name": "notAMetric"}]))
    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "Field notAMetric is not a valid metric.  For a list of valid "
        "dimensions and metrics, see https://developers.google.com/analytics/"
        "devguides/reporting/data/v1/api-schema "
    )


def test_dimension_and_metric_bounds(client, bearer):
    ten_dims = [
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
    response = client.post(PATH, headers=bearer, json=_body(dimensions=ten_dims))
    assert response.status_code == 400
    assert "9 dimensions" in response.json()["error"]["message"]

    eleven_metrics = [
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
    response = client.post(PATH, headers=bearer, json=_body(metrics=eleven_metrics))
    assert response.status_code == 400
    assert "10 metrics" in response.json()["error"]["message"]


def test_excessive_limit_silently_capped(client, bearer):
    report = _report(client, bearer, _body(limit=300_000))
    assert report["rowCount"] == 7


def test_keep_empty_rows_calendar_spine(client, bearer):
    """With a filter that matches nothing, keepEmptyRows must still render
    the calendar spine — the BI "series without gaps" case."""
    report = _report(
        client,
        bearer,
        _body(
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
    assert report["rowCount"] == 5
    assert all(row["metricValues"][0]["value"] == "0" for row in report["rows"])


def test_property_quota(client, bearer):
    """The SIX buckets of the PropertyQuota message, no more, no less.

    The list is the one from the v1beta discovery document (rev. 20260831):
    missing one — `tokensPerProjectPerHour` was missing until the first
    replay against the real service — makes the consumer believe a cap
    doesn't exist, when it's precisely the one that stops it first.
    """
    first = _report(client, bearer, _body(returnPropertyQuota=True))
    quota1 = first["propertyQuota"]
    assert set(quota1) == {
        "tokensPerDay",
        "tokensPerHour",
        "tokensPerProjectPerHour",
        "concurrentRequests",
        "serverErrorsPerProjectPerHour",
        "potentiallyThresholdedRequestsPerHour",
    }
    second = _report(client, bearer, _body(returnPropertyQuota=True))
    quota2 = second["propertyQuota"]
    # A report is counted against ALL token buckets at once. A short,
    # narrow report costs ONE token — measured on the real service.
    for bucket in ("tokensPerDay", "tokensPerHour", "tokensPerProjectPerHour"):
        assert quota1[bucket]["consumed"] == 1
        assert quota1[bucket]["remaining"] - quota2[bucket]["remaining"] == 1
    # Buckets a request doesn't consume still render BOTH fields:
    # `consumed: 0` is present, contrary to the usual proto3 rule. Found on
    # the service.
    assert quota1["concurrentRequests"] == {"consumed": 0, "remaining": 10}
    assert quota1["serverErrorsPerProjectPerHour"] == {"consumed": 0, "remaining": 10}
    assert quota1["potentiallyThresholdedRequestsPerHour"] == {"consumed": 0, "remaining": 120}


def test_project_hour_bucket_is_35_percent_of_the_hourly_bucket(client, bearer):
    """« Analytics Properties can use up to 35% of their tokens per project
    per hour » — 14 000 versus 40 000, vendor defaults, confirmed on a real
    property."""
    quota = _report(client, bearer, _body(returnPropertyQuota=True))["propertyQuota"]
    assert quota["tokensPerHour"]["remaining"] == 40_000 - 1
    assert quota["tokensPerProjectPerHour"]["remaining"] == 14_000 - 1


def test_token_cost_grows_with_the_request(client, bearer):
    """The cost is NOT flat — model calibrated on seven real measurements: a
    narrow report over 30 days costs 1, the same over 365 days costs 7, and
    9 dimensions by 10 metrics over 30 days cost 4."""

    def cost(**overrides):
        report = _report(client, bearer, _body(returnPropertyQuota=True, **overrides))
        return report["propertyQuota"]["tokensPerDay"]["consumed"]

    month = [{"startDate": "2026-06-01", "endDate": "2026-06-30"}]
    year = [{"startDate": "2025-07-16", "endDate": "2026-07-15"}]
    nine_dims = [
        {"name": n}
        for n in (
            "date",
            "country",
            "city",
            "browser",
            "deviceCategory",
            "sessionSource",
            "sessionMedium",
            "pagePath",
            "eventName",
        )
    ]
    ten_metrics = [
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
            "screenPageViews",
            "eventCount",
        )
    ]
    assert cost(dateRanges=month) == 1
    assert cost(dateRanges=year) == 7
    assert cost(dateRanges=month, dimensions=nine_dims, metrics=ten_metrics, limit=10) == 4


def test_cohorts_explicitly_rejected(client, bearer):
    response = client.post(PATH, headers=bearer, json=_body(cohortSpec={}))
    assert response.status_code == 400
    assert "not supported" in response.json()["error"]["message"]


def test_default_order_is_deterministic(client, bearer):
    body = _body(
        dimensions=[{"name": "sessionSource"}],
        metrics=[{"name": "sessions"}, {"name": "totalUsers"}],
    )
    first = _report(client, bearer, body)
    second = _report(client, bearer, body)
    assert first["rows"] == second["rows"]
    values = [int(row["metricValues"][0]["value"]) for row in first["rows"]]
    assert values == sorted(values, reverse=True)


def test_orderbys_dimension_and_metric(client, bearer):
    descending = _report(
        client,
        bearer,
        _body(orderBys=[{"desc": True, "dimension": {"dimensionName": "date"}}]),
    )
    dates = [row["dimensionValues"][0]["value"] for row in descending["rows"]]
    assert dates == sorted(dates, reverse=True)

    ascending = _report(
        client,
        bearer,
        _body(orderBys=[{"metric": {"metricName": "sessions"}}]),
    )
    values = [int(row["metricValues"][0]["value"]) for row in ascending["rows"]]
    assert values == sorted(values)


def test_orderby_outside_the_report_rejected(client, bearer):
    response = client.post(
        PATH,
        headers=bearer,
        json=_body(orderBys=[{"dimension": {"dimensionName": "country"}}]),
    )
    assert response.status_code == 400


def test_batch_run_reports(client, bearer):
    response = client.post(
        PATH_BATCH,
        headers=bearer,
        json={"requests": [_body(), _body(metrics=[{"name": "totalUsers"}])]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "analyticsData#batchRunReports"
    assert len(body["reports"]) == 2
    assert body["reports"][0]["kind"] == "analyticsData#runReport"

    too_many = client.post(
        PATH_BATCH, headers=bearer, json={"requests": [_body() for _ in range(6)]}
    )
    assert too_many.status_code == 400
    assert "5 requests" in too_many.json()["error"]["message"]


def test_reversed_range_and_invalid_date(client, bearer):
    reversed_ = client.post(
        PATH,
        headers=bearer,
        json=_body(dateRanges=[{"startDate": "2026-06-07", "endDate": "2026-06-01"}]),
    )
    assert reversed_.status_code == 400

    invalid = client.post(
        PATH,
        headers=bearer,
        json=_body(dateRanges=[{"startDate": "01/06/2026", "endDate": "2026-06-07"}]),
    )
    assert reversed_.json()["error"]["message"] == (
        "start_date must be less than or equal to end_date. "
        "start_date = 2026-06-07 and end_date = 2026-06-01"
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["message"] == (
        "Invalid startDate : 01/06/2026. startDate must be YYYY-MM-DD, "
        "NdaysAgo, yesterday, or today."
    )

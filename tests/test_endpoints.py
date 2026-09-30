"""HTTP surface — A1 milestone version, expanded in later milestones."""

import pytest


def test_health_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ga-mock"}


MINIMAL_BODY = {
    "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
    "metrics": [{"name": "sessions"}],
}


def test_colon_pattern_captures_the_property(client, bearer):
    """The `:runReport` literal after {property_id} must route — and the
    parameter must contain the id WITHOUT the suffix (proof: the property
    check accepts the right id and rejects another)."""
    ok = client.post("/v1beta/properties/424242001:runReport", headers=bearer, json=MINIMAL_BODY)
    assert ok.status_code == 200
    other = client.post("/v1beta/properties/999999999:runReport", headers=bearer, json=MINIMAL_BODY)
    assert other.status_code == 403
    assert other.json()["error"]["status"] == "PERMISSION_DENIED"


def test_non_numeric_property(client, bearer):
    response = client.post("/v1beta/properties/abc:runReport", headers=bearer, json=MINIMAL_BODY)
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"


def test_metadata_derived_from_the_registry(client, bearer):
    response = client.get("/v1beta/properties/424242001/metadata", headers=bearer)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "properties/424242001/metadata"
    dimensions = {d["apiName"] for d in body["dimensions"]}
    metrics = {m["apiName"]: m for m in body["metrics"]}
    assert "date" in dimensions and "pagePath" in dimensions
    assert metrics["sessions"]["type"] == "TYPE_INTEGER"
    assert metrics["engagementRate"]["type"] == "TYPE_FLOAT"
    assert metrics["averageSessionDuration"]["type"] == "TYPE_SECONDS"
    # `keyEvents` as TYPE_FLOAT: it used to be a guess, it's now confirmed
    # on a real property.
    assert metrics["keyEvents"]["type"] == "TYPE_FLOAT"
    # proto3: `customDefinition` false is ABSENT, not rendered as false. The
    # service never writes it for standard fields.
    assert all("customDefinition" not in d for d in body["dimensions"])
    assert all("customDefinition" not in m for m in body["metrics"])
    # Old names the service still advertises, unchanged.
    dimensions = {d["apiName"]: d for d in body["dimensions"]}
    assert dimensions["dayOfWeek"]["deprecatedApiNames"] == ["dayOfWeekZero"]
    assert dimensions["sessionDefaultChannelGroup"]["deprecatedApiNames"] == [
        "sessionDefaultChannelGrouping"
    ]
    assert metrics["keyEvents"]["deprecatedApiNames"] == ["conversions"]
    assert metrics["sessionKeyEventRate"]["deprecatedApiNames"] == ["sessionConversionRate"]
    assert "deprecatedApiNames" not in metrics["sessions"]
    # Categories and UI names, also confirmed on the service.
    assert dimensions["sessionSource"]["category"] == "Traffic Source"
    assert dimensions["newVsReturning"]["category"] == "User"
    assert metrics["userEngagementDuration"]["category"] == "User"
    assert metrics["sessionKeyEventRate"]["category"] == "Session"
    assert metrics["sessionsPerUser"]["uiName"] == "Sessions per active user"


def test_metadata_property_zero_allowed(client, bearer):
    response = client.get("/v1beta/properties/0/metadata", headers=bearer)
    assert response.status_code == 200
    assert response.json()["name"] == "properties/0/metadata"


def test_metadata_other_property_rejected(client, bearer):
    response = client.get("/v1beta/properties/123456/metadata", headers=bearer)
    assert response.status_code == 403


def test_unknown_route_renders_html_not_json(client):
    """ROUTING doesn't speak google.rpc.

    Observed on analyticsdata.googleapis.com on 2026-09-02: an unknown path
    renders the gateway's HTML 404 page. The mock imitates it so the
    consumer discovers HERE that `response.json()` can fail — and that
    there is therefore not a SINGLE error shape on this API.
    """
    response = client.get("/v1beta/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    with pytest.raises(ValueError):
        response.json()


def test_unknown_verb_renders_html_not_json(client):
    response = client.post("/v1beta/properties/424242001:runNothing", json={})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_wrong_verb_is_a_404_not_a_405(client):
    """The vendor NEVER renders 405: the HTTP method is part of the route
    pattern, so a wrong verb is a route that doesn't exist. `GET` on
    `:runReport` does render 404 + HTML on the real service."""
    response = client.get("/v1beta/properties/424242001:runReport")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_mock_affordances_keep_a_readable_error(client):
    """The gateway's HTML page is reserved for VENDOR paths: a typo on
    /__admin or /health must speak to the developer."""
    for path in ("/__admin/stat", "/health/typo"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert "unknown mock path" in response.json()["error"]


def test_metadata_carries_the_standard_comparisons(client, bearer):
    """GA4 ships nine comparisons out of the box on any property — but NONE
    on `properties/0`, which only describes the common schema."""
    property_ = client.get("/v1beta/properties/424242001/metadata", headers=bearer).json()
    zero = client.get("/v1beta/properties/0/metadata", headers=bearer).json()
    names = [c["apiName"] for c in property_["comparisons"]]
    assert names[0] == "comparisons/allUsers"
    assert len(names) == 9
    assert all(n.startswith("comparisons/") for n in names)
    assert "comparisons" not in zero


def test_unknown_json_field_rejected_with_its_violations(client, bearer):
    """The transcoding layer rejects an unknown key BEFORE the method:
    `dateRange` instead of `dateRanges` must break here, not pass silently.
    Every offending key is listed, one violation each."""
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "metrics": [{"name": "sessions"}],
            "notAField": 1,
            "neitherIsThis": 2,
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    assert error["message"] == (
        'Invalid JSON payload received. Unknown name "notAField": Cannot find field.\n'
        'Invalid JSON payload received. Unknown name "neitherIsThis": Cannot find field.'
    )
    violations = error["details"][0]["fieldViolations"]
    assert error["details"][0]["@type"] == "type.googleapis.com/google.rpc.BadRequest"
    assert len(violations) == 2
    # No `field` here: the layer can't name a field that doesn't exist.
    assert all(set(v) == {"description"} for v in violations)


def test_unknown_nested_field_names_the_proto_path(client, bearer):
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02", "zzz": 1}],
            "metrics": [{"name": "sessions"}],
        },
    )
    assert response.status_code == 400
    # snake_case: this is the PROTO field name, not the JSON key that was sent.
    assert response.json()["error"]["message"] == (
        "Invalid JSON payload received. Unknown name \"zzz\" at 'date_ranges[0]': "
        "Cannot find field."
    )


def test_aggregation_outside_enum_names_the_field(client, bearer):
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "metrics": [{"name": "sessions"}],
            "metricAggregations": ["AVERAGE"],
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    expected = (
        "Invalid value at 'metric_aggregations[0]' "
        '(type.googleapis.com/google.analytics.data.v1beta.MetricAggregation), "AVERAGE"'
    )
    assert error["message"] == expected
    violation = error["details"][0]["fieldViolations"][0]
    assert violation == {"field": "metric_aggregations[0]", "description": expected}


def test_count_is_in_the_enum_but_rejected(client, bearer):
    """`COUNT` is a legal proto value — the service rejects it anyway, and
    with a METHOD-level message (no `details`), not a transcoding one."""
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        json={
            "dateRanges": [{"startDate": "2026-06-01", "endDate": "2026-06-02"}],
            "metrics": [{"name": "sessions"}],
            "metricAggregations": ["COUNT"],
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == "Metric aggregation Count is not supported in ReportRequest."
    assert "details" not in error


def test_service_date_bounds(client, bearer):
    """`start_date` must be STRICTLY later than 2015-08-13: 2015-08-13 itself
    is rejected, 2015-08-14 passes."""

    def status(start):
        return client.post(
            "/v1beta/properties/424242001:runReport",
            headers=bearer,
            json={
                "dateRanges": [{"startDate": start, "endDate": "2015-09-01"}],
                "metrics": [{"name": "sessions"}],
            },
        )

    rejected = status("2015-08-13")
    assert rejected.status_code == 400
    assert rejected.json()["error"]["message"] == (
        "start_date = 2015-08-13 must be greater than 2015-08-13 and less than 3000-01-01."
    )
    assert status("2015-08-14").status_code == 200


def test_empty_body_is_an_empty_message_not_a_parse_error(client, bearer):
    """Confirmed: an EMPTY body isn't rejected by the parser, it passes
    through as an empty message and fails validation. Nobody guesses this."""
    response = client.post("/v1beta/properties/424242001:runReport", headers=bearer, content=b"")
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "A dateRange is required."


def test_non_object_root_has_its_own_message(client, bearer):
    for payload in (b"null", b"[]"):
        response = client.post(
            "/v1beta/properties/424242001:runReport", headers=bearer, content=payload
        )
        assert response.status_code == 400
        assert response.json()["error"]["message"] == (
            'Invalid JSON payload received. Unknown name "": Root element must be a message.'
        )


def test_broken_json_renders_a_multiline_message_with_a_caret(client, bearer):
    """Three lines: the reason, the offending line, a caret under the
    column. The reason's WORDING comes from this parser (a recorded
    approximation), the geometry is the vendor's."""
    response = client.post(
        "/v1beta/properties/424242001:runReport",
        headers=bearer,
        content=b"{this isn't json",
    )
    assert response.status_code == 400
    lines = response.json()["error"]["message"].split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("Invalid JSON payload received. ")
    assert lines[1] == "{this isn't json"
    assert lines[2].endswith("^") and set(lines[2][:-1]) <= {" "}


def test_service_check_order(client, bearer):
    """The order confirmed on the service, with doubly-faulty cases as
    proof: a valid NAME and a positive `limit` take precedence over
    requiring a range, but cardinality bounds come AFTER it."""
    path = "/v1beta/properties/424242001:runReport"
    ten_dims = [
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

    def message(body):
        response = client.post(path, headers=bearer, json=body)
        assert response.status_code == 400, response.text
        return response.json()["error"]["message"]

    # invalid name + no range -> the NAME comes out
    assert "is not a valid dimension." in message({"dimensions": [{"name": "notADim"}]})
    # negative limit + no range -> LIMIT comes out
    assert message({"metrics": [{"name": "sessions"}], "limit": -1}) == (
        "limit must be positive. The API received limit = -1"
    )
    # ten dimensions + no range -> the RANGE comes out
    assert message({"dimensions": ten_dims}) == "A dateRange is required."
    # unknown JSON field -> transcoding comes before everything else
    assert message({"zzz": 1}).startswith('Invalid JSON payload received. Unknown name "zzz"')

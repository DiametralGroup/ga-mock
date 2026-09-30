"""The committed contract IS the one the app generates.

A contract that drifts silently lies to consumers (insights360 keeps a
pinned copy). `make contract` regenerates it; this test fails until the
diff is committed — a change in shape is a change in contract.
"""

from pathlib import Path

import yaml

import ga_mock
from ga_mock.models import UNVERIFIED_BEHAVIORS

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "ga4-data.openapi.yaml"
REGISTRY = ROOT / "docs" / "UNVERIFIED-FIELDS.md"


def test_committed_contract_is_up_to_date():
    committed = yaml.safe_load(CONTRACT.read_text())
    assert committed == ga_mock.contract_openapi(), (
        "contracts/ga4-data.openapi.yaml differs from the app — run `make contract` "
        "and READ the diff before committing"
    )


def test_contract_carries_the_real_shapes():
    contract = ga_mock.contract_openapi()
    paths = contract["paths"]
    assert set(paths) == {
        "/token",
        "/v1beta/properties/{property_id}:runReport",
        "/v1beta/properties/{property_id}:batchRunReports",
        "/v1beta/properties/{property_id}/metadata",
    }
    assert len(contract["components"]["schemas"]) > 15
    operation = paths["/v1beta/properties/{property_id}:runReport"]["post"]
    response_200 = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in response_200
    assert "requestBody" in operation


def test_mock_affordances_are_out_of_contract():
    contract = ga_mock.contract_openapi()
    for path in contract["paths"]:
        assert not path.startswith("/__admin")
        assert not path.startswith("/__fixtures")
        assert path != "/health"
    # FastAPI's 422s don't exist on the vendor side
    for operations in contract["paths"].values():
        for operation in operations.values():
            assert "422" not in operation.get("responses", {})


def test_errors_are_documented():
    operation = ga_mock.contract_openapi()["paths"]["/v1beta/properties/{property_id}:runReport"][
        "post"
    ]
    assert {"400", "401", "403", "429"} <= set(operation["responses"])


def test_unverified_registry_is_complete():
    """Every approximated behavior and every contract marker must be
    documented — inventing without saying so is a build failure."""
    registry = REGISTRY.read_text()
    for identifier in UNVERIFIED_BEHAVIORS:
        assert f"`{identifier}`" in registry, f"{identifier} missing from the registry"

    def markers(obj):
        if isinstance(obj, dict):
            if obj.get("x-ga-confidence") == "unverified":
                yield obj
            for value in obj.values():
                yield from markers(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from markers(value)

    found = list(markers(ga_mock.contract_openapi()))
    assert found, "the contract must carry x-ga-confidence markers"
    assert "x-ga-confidence" in registry

"""Le contrat committé EST celui que l'application génère.

Un contrat qui dérive en silence ment aux consommateurs (insights360 en garde
une copie épinglée). `make contract` régénère ; ce test échoue tant que le
diff n'est pas committé — un changement de forme est un changement de contrat.
"""

from pathlib import Path

import yaml

import ga_mock
from ga_mock.models import UNVERIFIED_BEHAVIORS

RACINE = Path(__file__).resolve().parent.parent
CONTRAT = RACINE / "contracts" / "ga4-data.openapi.yaml"
REGISTRE = RACINE / "docs" / "UNVERIFIED-FIELDS.md"


def test_le_contrat_committe_est_a_jour():
    committe = yaml.safe_load(CONTRAT.read_text())
    assert committe == ga_mock.contract_openapi(), (
        "contracts/ga4-data.openapi.yaml diffère de l'app — lancer `make contract` "
        "et RELIRE le diff avant de committer"
    )


def test_le_contrat_porte_les_vraies_formes():
    contrat = ga_mock.contract_openapi()
    chemins = contrat["paths"]
    assert set(chemins) == {
        "/token",
        "/v1beta/properties/{property_id}:runReport",
        "/v1beta/properties/{property_id}:batchRunReports",
        "/v1beta/properties/{property_id}/metadata",
    }
    assert len(contrat["components"]["schemas"]) > 15
    operation = chemins["/v1beta/properties/{property_id}:runReport"]["post"]
    reponse_200 = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in reponse_200
    assert "requestBody" in operation


def test_les_affordances_du_mock_sont_hors_contrat():
    contrat = ga_mock.contract_openapi()
    for chemin in contrat["paths"]:
        assert not chemin.startswith("/__admin")
        assert not chemin.startswith("/__fixtures")
        assert chemin != "/health"
    # les 422 FastAPI n'existent pas chez le vendeur
    for operations in contrat["paths"].values():
        for operation in operations.values():
            assert "422" not in operation.get("responses", {})


def test_les_erreurs_sont_documentees():
    operation = ga_mock.contract_openapi()["paths"]["/v1beta/properties/{property_id}:runReport"][
        "post"
    ]
    assert {"400", "401", "403", "429"} <= set(operation["responses"])


def test_le_registre_unverified_est_complet():
    """Chaque comportement approximé et chaque marqueur du contrat doivent
    être documentés — inventer sans le dire est une faute de build."""
    registre = REGISTRE.read_text()
    for identifiant in UNVERIFIED_BEHAVIORS:
        assert f"`{identifiant}`" in registre, f"{identifiant} absent du registre"

    def marqueurs(objet):
        if isinstance(objet, dict):
            if objet.get("x-ga-confidence") == "unverified":
                yield objet
            for valeur in objet.values():
                yield from marqueurs(valeur)
        elif isinstance(objet, list):
            for valeur in objet:
                yield from marqueurs(valeur)

    trouves = list(marqueurs(ga_mock.contract_openapi()))
    assert trouves, "le contrat doit porter des marqueurs x-ga-confidence"
    assert "x-ga-confidence" in registre

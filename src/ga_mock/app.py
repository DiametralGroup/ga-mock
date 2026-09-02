"""Assemblage de l'application.

Le mock sert TROIS surfaces sur un seul serveur :
  • `/token` — l'équivalent de oauth2.googleapis.com (flux service account) ;
  • `/v1beta/...` — l'équivalent de analyticsdata.googleapis.com ;
  • le hors-contrat : `/health`, `/__fixtures/*`, `/__admin/*` (jamais dans
    l'OpenAPI publié).

En prod ce sont deux hôtes Google distincts — le consommateur configure donc
DEUX URLs (`GA_API_URL`, `GA_TOKEN_URL`) ; ici un seul process suffit, les
chemins ne se recouvrent pas.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import (
    GRANT_TYPE_JWT_BEARER,
    ErreurToken,
    bearer_valide,
    fixture_service_account,
    reponse_token,
    valider_assertion,
)
from .errors import (
    MESSAGE_401_INVALIDE,
    MESSAGE_401_MANQUANT,
    MESSAGE_403_PROPRIETE,
    MESSAGE_403_PROPRIETE_INVALIDE,
    WWW_AUTHENTICATE_401_INVALIDE,
    WWW_AUTHENTICATE_401_MANQUANT,
    ErreurQuota,
    detail_credentials_manquantes,
    erreur,
    page_html_404,
)
from .injection import engine
from .models import (
    REPONSES_ERREUR,
    BatchRunReportsRequest,
    BatchRunReportsResponse,
    MetadataResponse,
    OAuthErrorResponse,
    RunReportRequest,
    RunReportResponse,
    TokenResponse,
    corps_requete,
)
from .registry import metadata_payload
from .report import ErreurRequete, executer_run_report
from .settings import settings

app = FastAPI(title="Google Analytics 4 mock", version="0.1.0", docs_url="/docs")


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Unauthenticated — it is a probe, not an API surface."""
    return {"status": "ok", "service": "ga-mock"}


def _pre_traitement(request: Request) -> JSONResponse | None:
    """Le point UNIQUE d'injection, évalué AVANT l'authentification : une
    `auth_reject` doit pouvoir préempter un bearer valide, une `latency`
    s'appliquer même à un appel token."""
    chemin = request.url.path
    engine.observer(chemin)
    return engine.evaluer(chemin)


def _resume_rapport(corps: dict[str, object]) -> dict[str, object]:
    return {
        cle: corps[cle]
        for cle in ("dateRanges", "dimensions", "metrics", "limit", "offset")
        if cle in corps
    }


# Ce qui n'est PAS le vendeur : ces chemins existent pour le développeur, et
# leurs erreurs doivent lui parler — pas imiter une passerelle Google.
CHEMINS_AFFORDANCE = ("/health", "/__admin", "/__fixtures", "/docs", "/openapi.json")

# Noms gRPC complets des méthodes, tels que le vendeur les inscrit dans le
# `details` d'un 401 « credential manquant ».
METHODE_RPC = {
    "runReport": "google.analytics.data.v1beta.BetaAnalyticsData.RunReport",
    "batchRunReports": "google.analytics.data.v1beta.BetaAnalyticsData.BatchRunReports",
    "getMetadata": "google.analytics.data.v1beta.BetaAnalyticsData.GetMetadata",
}


def _verifier_bearer(request: Request, methode_rpc: str) -> JSONResponse | None:
    """Garde des endpoints DATA — enveloppe google.rpc, pas RFC 6749.

    Seul `/token` parle le dialecte OAuth2 ; tout le reste de la surface
    répond comme analyticsdata.googleapis.com.

    DEUX refus distincts, attestés : en-tête absent → « missing required
    authentication credential », avec un `details` google.rpc.ErrorInfo et un
    `WWW-Authenticate` SANS `error=` ; jeton présent mais refusé → « had
    invalid authentication credentials », sans `details`, avec
    `error="invalid_token"`. Le client qui ne sait pas s'il doit s'authentifier
    ou RENOUVELER lit exactement cette différence.
    """
    autorisation = request.headers.get("Authorization", "")
    if not autorisation:
        return erreur(
            401,
            MESSAGE_401_MANQUANT,
            details=detail_credentials_manquantes(methode_rpc),
            headers={"WWW-Authenticate": WWW_AUTHENTICATE_401_MANQUANT},
        )
    if autorisation.startswith("Bearer ") and bearer_valide(autorisation[7:]):
        return None
    return erreur(
        401, MESSAGE_401_INVALIDE, headers={"WWW-Authenticate": WWW_AUTHENTICATE_401_INVALIDE}
    )


@app.post(
    "/token",
    tags=["oauth2"],
    response_model=TokenResponse,
    responses={
        400: {
            "model": OAuthErrorResponse,
            "description": "unsupported_grant_type / invalid_grant / invalid_scope",
        }
    },
    summary="Service-account JWT-bearer token exchange",
)
async def token(request: Request) -> JSONResponse:
    """Endpoint oauth2.googleapis.com/token — flux service account JWT-bearer.

    Le corps est parsé à la main (urllib) plutôt que via `request.form()` :
    Starlette délègue les formulaires à python-multipart, et un corps
    urlencoded ne justifie pas d'élargir les dépendances runtime.
    """
    if (panne := _pre_traitement(request)) is not None:
        return panne
    brut = (await request.body()).decode("utf-8", errors="replace")
    champs = {cle: valeurs[-1] for cle, valeurs in parse_qs(brut, keep_blank_values=True).items()}
    if champs.get("grant_type") != GRANT_TYPE_JWT_BEARER:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_grant_type",
                "error_description": f"Invalid grant_type: {champs.get('grant_type', '')}",
            },
        )
    try:
        claims = valider_assertion(champs.get("assertion", ""))
    except ErreurToken as exc:
        return JSONResponse(
            status_code=400,
            content={"error": exc.code, "error_description": exc.description},
        )
    # 200 ne veut pas dire `access_token` : un scope non reconnu rend un
    # id_token SEUL, comme le vrai endpoint.
    return JSONResponse(reponse_token(claims))


@app.get("/__fixtures/service-account.json", include_in_schema=False)
def fixture_sa(request: Request) -> JSONResponse:
    """Hors contrat, comme /__fixtures/remuneration.csv chez boondmanager-mock :
    une commodité de dev, pas un chemin Google."""
    return JSONResponse(fixture_service_account(str(request.base_url).rstrip("/")))


def _verifier_propriete(property_id: str, *, zero_admis: bool = False) -> JSONResponse | None:
    """400 sur un identifiant non numérique, 403 sur une AUTRE propriété : le
    jeton du mock n'a de droits que sur la propriété configurée — même
    comportement qu'un compte de service réel au périmètre étroit."""
    if not property_id.isdigit():
        return erreur(400, MESSAGE_403_PROPRIETE_INVALIDE.format(id=property_id))
    admis = {settings.property_id, "0"} if zero_admis else {settings.property_id}
    if property_id not in admis:
        return erreur(403, MESSAGE_403_PROPRIETE)
    return None


async def _corps_json(request: Request) -> dict[str, object] | JSONResponse:
    """Le corps, au dialecte du transcodeur JSON du vendeur — relevé cas par cas.

    Trois comportements que personne ne devine :
      • un corps VIDE n'est pas une erreur de parsing, c'est un message vide —
        la requête part en validation et échoue sur `A dateRange is required.` ;
      • une racine qui n'est pas un objet (`null`, `[]`) a son propre message,
        qui parle de « Root element » et non de syntaxe ;
      • une syntaxe cassée rend un message MULTILIGNE : la raison, puis la
        ligne fautive, puis un accent circonflexe sous la colonne. Un
        consommateur qui journalise ce message verra trois lignes en prod.
    """
    brut = (await request.body()).decode("utf-8", errors="replace")
    if not brut:
        return {}
    try:
        corps = json.loads(brut)
    except ValueError as exc:
        return erreur(400, _message_json_invalide(brut, exc))
    if not isinstance(corps, dict):
        return erreur(
            400,
            'Invalid JSON payload received. Unknown name "": Root element must be a message.',
        )
    return corps


def _message_json_invalide(brut: str, exc: ValueError) -> str:
    """Reproduit la FORME du message du transcodeur : raison, extrait, caret.

    Le libellé exact de la raison vient du parseur C++ de protobuf (« Expected
    : between key:value pair. ») et n'est pas reproductible depuis Python ;
    c'est celui du parseur d'ici qui sert, et l'approximation est consignée
    (`messages-erreurs-validation`). La géométrie — trois lignes, caret sous la
    colonne fautive — est, elle, fidèle.
    """
    ligne, colonne = getattr(exc, "lineno", 1), getattr(exc, "colno", 1)
    lignes = brut.splitlines() or [""]
    extrait = lignes[ligne - 1] if 0 < ligne <= len(lignes) else ""
    raison = str(getattr(exc, "msg", exc)).split(":")[0]
    return f"Invalid JSON payload received. {raison}.\n{extrait}\n{' ' * (colonne - 1)}^"


# Le pattern « :verbe » du transcodage gRPC marche tel quel dans Starlette : le
# littéral `:runReport` suit le paramètre dans le MÊME segment d'URL, et le
# backtracking de la regex compilée sépare correctement les deux. Vérifié
# empiriquement avant d'écrire la moindre logique dessus.
@app.post(
    "/v1beta/properties/{property_id}:runReport",
    response_model=RunReportResponse,
    responses=REPONSES_ERREUR,
    openapi_extra=corps_requete(RunReportRequest),
    summary="Run a report",
)
async def run_report(request: Request, property_id: str) -> JSONResponse:
    if (panne := _pre_traitement(request)) is not None:
        return panne
    if (refus := _verifier_bearer(request, METHODE_RPC["runReport"])) is not None:
        return refus
    if (refus := _verifier_propriete(property_id)) is not None:
        return refus
    corps = await _corps_json(request)
    if isinstance(corps, JSONResponse):
        return corps
    engine.noter_corps(request.url.path, _resume_rapport(corps))
    return _executer_protege(corps)


def _executer_protege(corps: dict[str, object]) -> JSONResponse:
    try:
        return JSONResponse(executer_run_report(corps))
    except ErreurRequete as exc:
        return erreur(400, str(exc), details=exc.details)
    except ErreurQuota as exc:
        return erreur(429, str(exc))


def _executer_batch(corps: dict[str, object]) -> JSONResponse:
    demandes = corps.get("requests")
    if not isinstance(demandes, list) or not demandes:
        return erreur(400, "The batchRunReportsRequest must contain at least one runReportRequest.")
    if len(demandes) > 5:
        return erreur(
            400,
            "Batch requests are limited to 5 requests.\n"
            f"  This batch request contains {len(demandes)} requests.",
        )
    rapports = []
    try:
        for demande in demandes:
            if not isinstance(demande, dict):
                return erreur(400, "Invalid value for requests.")
            rapports.append(executer_run_report(demande))
    except ErreurRequete as exc:
        return erreur(400, str(exc), details=exc.details)
    except ErreurQuota as exc:
        return erreur(429, str(exc))
    return JSONResponse({"reports": rapports, "kind": "analyticsData#batchRunReports"})


@app.post(
    "/v1beta/properties/{property_id}:batchRunReports",
    response_model=BatchRunReportsResponse,
    responses=REPONSES_ERREUR,
    openapi_extra=corps_requete(BatchRunReportsRequest),
    summary="Run up to 5 reports in one call",
)
async def batch_run_reports(request: Request, property_id: str) -> JSONResponse:
    """≤ 5 sous-rapports ; l'auth, la propriété et le JSON se contrôlent UNE
    fois au niveau du lot, puis chaque sous-rapport traverse le pipeline
    complet — une sous-requête invalide fait échouer tout le lot."""
    if (panne := _pre_traitement(request)) is not None:
        return panne
    if (refus := _verifier_bearer(request, METHODE_RPC["batchRunReports"])) is not None:
        return refus
    if (refus := _verifier_propriete(property_id)) is not None:
        return refus
    corps = await _corps_json(request)
    if isinstance(corps, JSONResponse):
        return corps
    demandes = corps.get("requests")
    if isinstance(demandes, list):
        engine.noter_corps(
            request.url.path,
            {"requests": [_resume_rapport(d) for d in demandes if isinstance(d, dict)]},
        )
    return _executer_batch(corps)


@app.get(
    "/v1beta/properties/{property_id}/metadata",
    response_model=MetadataResponse,
    responses=REPONSES_ERREUR,
    summary="Dimensions and metrics available on the property",
)
def metadata(request: Request, property_id: str) -> JSONResponse:
    """L'auto-description de la propriété — générée DEPUIS le registre.

    `properties/0/metadata` est admis, comme chez Google : le zéro désigne les
    métadonnées communes à toutes les propriétés.
    """
    if (panne := _pre_traitement(request)) is not None:
        return panne
    if (refus := _verifier_bearer(request, METHODE_RPC["getMetadata"])) is not None:
        return refus
    if (refus := _verifier_propriete(property_id, zero_admis=True)) is not None:
        return refus
    return JSONResponse(metadata_payload(property_id))


@app.exception_handler(StarletteHTTPException)
async def _erreur_http(request: Request, exc: StarletteHTTPException) -> Response:
    """Le ROUTAGE ne parle pas google.rpc — il ne parle même pas JSON.

    Attesté sur le vrai service : `POST …:runNothing`, `GET /v1beta/pasUne`
    et `GET …:runReport` (mauvais verbe) rendent TOUS les trois une page HTML
    404 de la passerelle, `text/html; charset=UTF-8`. Il n'y a pas de 405 :
    la méthode HTTP fait partie du motif de route, donc un mauvais verbe est
    simplement une route qui n'existe pas.

    C'est le seul endroit de la surface où `reponse.json()` échoue — et un
    consommateur doit l'apprendre ici, pas en prod.

    La page HTML est réservée aux chemins du VENDEUR : sur les affordances du
    mock (`/health`, `/__admin`, `/__fixtures`), une faute de frappe doit rester
    lisible pour un développeur, pas être déguisée en erreur Google.

    Les autres codes passent tels quels : masquer un vrai bug derrière une
    enveloppe polie serait pire que l'exposer.
    """
    if exc.status_code in (404, 405):
        if any(request.url.path.startswith(prefixe) for prefixe in CHEMINS_AFFORDANCE):
            return JSONResponse(
                status_code=404,
                content={"error": f"unknown mock path {request.url.path}"},
            )
        return page_html_404(request.url.path)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Monté SEULEMENT si activé : le plan de contrôle est ABSENT (pas simplement
# interdit) quand GA_MOCK_ADMIN_ENABLED ne le demande pas — il n'apparaît ni
# dans les routes ni dans le contrat OpenAPI publié.
if settings.admin_enabled:
    from .admin import router as admin_router

    app.include_router(admin_router)


# ── Contrat publié ───────────────────────────────────────────────────────────


def _references(objet: Any, refs: set[str]) -> None:
    if isinstance(objet, dict):
        ref = objet.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            refs.add(ref.rsplit("/", 1)[1])
        for valeur in objet.values():
            _references(valeur, refs)
    elif isinstance(objet, list):
        for valeur in objet:
            _references(valeur, refs)


def _elaguer_schemas_orphelins(schema: dict[str, Any]) -> None:
    """Fermeture transitive des $ref depuis les chemins gardés : retirer un
    chemin retire ses formes, y compris celles qu'il était seul à référencer."""
    composants = schema.get("components", {}).get("schemas", {})
    utiles: set[str] = set()
    _references(schema["paths"], utiles)
    while True:
        avant = len(utiles)
        for nom in list(utiles):
            if nom in composants:
                _references(composants[nom], utiles)
        if len(utiles) == avant:
            break
    restants = {nom: composants[nom] for nom in sorted(utiles) if nom in composants}
    if restants:
        schema["components"]["schemas"] = restants
    else:
        schema.pop("components", None)


def contract_openapi() -> dict[str, Any]:
    """Le contrat publié : les chemins du VENDEUR uniquement.

    /health, /__fixtures et /__admin sont des affordances du mock — les faire
    entrer dans le contrat serait mentir sur la surface Google. Les réponses
    422 auto-documentées par FastAPI sont retirées pour la même raison : le
    vendeur répond 400 INVALID_ARGUMENT, jamais un HTTPValidationError.
    """
    schema = deepcopy(app.openapi())
    schema["info"] = {
        "title": "Google Analytics Data API v1beta — ga-mock contract",
        "version": app.version,
        "description": (
            "Surface reproduced by ga-mock: the OAuth2 service-account token "
            "exchange and the GA4 Data API v1beta core (runReport, "
            "batchRunReports, metadata). Sources of truth: the public GA4 Data "
            "API v1beta REST reference and its discovery document. Fields "
            "marked x-ga-confidence: unverified are registered in "
            "docs/UNVERIFIED-FIELDS.md."
        ),
    }
    schema["paths"] = {
        chemin: operations
        for chemin, operations in schema.get("paths", {}).items()
        if chemin == "/token" or chemin.startswith("/v1beta/")
    }
    for operations in schema["paths"].values():
        for operation in operations.values():
            if isinstance(operation, dict):
                operation.get("responses", {}).pop("422", None)
    _elaguer_schemas_orphelins(schema)
    return schema

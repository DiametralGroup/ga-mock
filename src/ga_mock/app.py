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

from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import (
    GRANT_TYPE_JWT_BEARER,
    ErreurToken,
    bearer_valide,
    emettre_bearer,
    fixture_service_account,
    valider_assertion,
)
from .errors import MESSAGE_401, MESSAGE_403_PROPRIETE, ErreurQuota, erreur
from .injection import engine
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


def _verifier_bearer(request: Request) -> JSONResponse | None:
    """Garde des endpoints DATA — enveloppe google.rpc, pas RFC 6749.

    Seul `/token` parle le dialecte OAuth2 ; tout le reste de la surface
    répond comme analyticsdata.googleapis.com."""
    autorisation = request.headers.get("Authorization", "")
    if autorisation.startswith("Bearer ") and bearer_valide(autorisation[7:]):
        return None
    return erreur(401, MESSAGE_401)


@app.post("/token", tags=["oauth2"])
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
        valider_assertion(champs.get("assertion", ""))
    except ErreurToken as exc:
        return JSONResponse(
            status_code=400,
            content={"error": exc.code, "error_description": exc.description},
        )
    return JSONResponse(emettre_bearer())


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
        return erreur(400, f"Invalid property ID {property_id}.")
    admis = {settings.property_id, "0"} if zero_admis else {settings.property_id}
    if property_id not in admis:
        return erreur(403, MESSAGE_403_PROPRIETE)
    return None


async def _corps_json(request: Request) -> dict[str, object] | JSONResponse:
    try:
        corps = await request.json()
    except ValueError:
        return erreur(400, "Invalid JSON payload received.")
    if not isinstance(corps, dict):
        return erreur(400, "Invalid JSON payload received.")
    return corps


# Le pattern « :verbe » du transcodage gRPC marche tel quel dans Starlette : le
# littéral `:runReport` suit le paramètre dans le MÊME segment d'URL, et le
# backtracking de la regex compilée sépare correctement les deux. Vérifié
# empiriquement avant d'écrire la moindre logique dessus.
@app.post("/v1beta/properties/{property_id}:runReport")
async def run_report(request: Request, property_id: str) -> JSONResponse:
    if (panne := _pre_traitement(request)) is not None:
        return panne
    if (refus := _verifier_bearer(request)) is not None:
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
        return erreur(400, str(exc))
    except ErreurQuota as exc:
        return erreur(429, str(exc))


def _executer_batch(corps: dict[str, object]) -> JSONResponse:
    demandes = corps.get("requests")
    if not isinstance(demandes, list) or not demandes:
        return erreur(400, "batchRunReports must specify at least one request.")
    if len(demandes) > 5:
        return erreur(400, "batchRunReports is limited to 5 requests.")
    rapports = []
    try:
        for demande in demandes:
            if not isinstance(demande, dict):
                return erreur(400, "Invalid value for requests.")
            rapports.append(executer_run_report(demande))
    except ErreurRequete as exc:
        return erreur(400, str(exc))
    except ErreurQuota as exc:
        return erreur(429, str(exc))
    return JSONResponse({"reports": rapports, "kind": "analyticsData#batchRunReports"})


@app.post("/v1beta/properties/{property_id}:batchRunReports")
async def batch_run_reports(request: Request, property_id: str) -> JSONResponse:
    """≤ 5 sous-rapports ; l'auth, la propriété et le JSON se contrôlent UNE
    fois au niveau du lot, puis chaque sous-rapport traverse le pipeline
    complet — une sous-requête invalide fait échouer tout le lot."""
    if (panne := _pre_traitement(request)) is not None:
        return panne
    if (refus := _verifier_bearer(request)) is not None:
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


@app.get("/v1beta/properties/{property_id}/metadata")
def metadata(request: Request, property_id: str) -> JSONResponse:
    """L'auto-description de la propriété — générée DEPUIS le registre.

    `properties/0/metadata` est admis, comme chez Google : le zéro désigne les
    métadonnées communes à toutes les propriétés.
    """
    if (panne := _pre_traitement(request)) is not None:
        return panne
    if (refus := _verifier_bearer(request)) is not None:
        return refus
    if (refus := _verifier_propriete(property_id, zero_admis=True)) is not None:
        return refus
    return JSONResponse(metadata_payload(property_id))


@app.exception_handler(StarletteHTTPException)
async def _erreur_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Route inconnue ou mauvais verbe : l'enveloppe Google, jamais {"detail"}.

    Les autres codes passent tels quels — FastAPI n'en émet pas d'autres de
    lui-même sur ce mock, et masquer un vrai bug derrière une enveloppe polie
    serait pire que l'exposer.
    """
    if exc.status_code == 404:
        return erreur(404, f"Requested entity was not found: {request.url.path}.")
    if exc.status_code == 405:
        return erreur(405, f"Method {request.method} is not allowed on {request.url.path}.")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Monté SEULEMENT si activé : le plan de contrôle est ABSENT (pas simplement
# interdit) quand GA_MOCK_ADMIN_ENABLED ne le demande pas — il n'apparaît ni
# dans les routes ni dans le contrat OpenAPI publié.
if settings.admin_enabled:
    from .admin import router as admin_router

    app.include_router(admin_router)

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
from .errors import MESSAGE_401, erreur

app = FastAPI(title="Google Analytics 4 mock", version="0.1.0", docs_url="/docs")


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Unauthenticated — it is a probe, not an API surface."""
    return {"status": "ok", "service": "ga-mock"}


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


# Le pattern « :verbe » du transcodage gRPC marche tel quel dans Starlette : le
# littéral `:runReport` suit le paramètre dans le MÊME segment d'URL, et le
# backtracking de la regex compilée sépare correctement les deux. Vérifié
# empiriquement avant d'écrire la moindre logique dessus.
@app.post("/v1beta/properties/{property_id}:runReport")
def run_report(request: Request, property_id: str) -> JSONResponse:
    """Stub des jalons A1/A2 — remplacé par le vrai pipeline au jalon A4."""
    if (refus := _verifier_bearer(request)) is not None:
        return refus
    return erreur(501, f"runReport for properties/{property_id} is not implemented yet.")


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

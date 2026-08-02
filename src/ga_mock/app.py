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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import erreur

app = FastAPI(title="Google Analytics 4 mock", version="0.1.0", docs_url="/docs")


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Unauthenticated — it is a probe, not an API surface."""
    return {"status": "ok", "service": "ga-mock"}


# Le pattern « :verbe » du transcodage gRPC marche tel quel dans Starlette : le
# littéral `:runReport` suit le paramètre dans le MÊME segment d'URL, et le
# backtracking de la regex compilée sépare correctement les deux. Vérifié
# empiriquement avant d'écrire la moindre logique dessus.
@app.post("/v1beta/properties/{property_id}:runReport")
def run_report(property_id: str) -> JSONResponse:
    """Stub du jalon A1 — remplacé par le vrai pipeline au jalon A4."""
    return erreur(
        501,
        f"runReport for properties/{property_id} is not implemented yet.",
    )


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

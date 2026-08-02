"""Plan de contrôle /__admin — l'outillage du mock, jamais un chemin Google.

Monté à la RACINE (pas sous /v1beta) pour qu'un pare-feu puisse le bloquer en
bloc, et SEULEMENT quand GA_MOCK_ADMIN_ENABLED=true : absent, pas simplement
interdit. Il existe parce que les consommateurs font tourner le mock en
CONTENEUR (compose, sidecar CI) : sans HTTP, plus aucun moyen de réinitialiser
l'état, d'avancer l'horloge ou d'injecter une panne entre deux tests.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .clock import horloge, virtual_now
from .injection import KINDS, engine
from .settings import settings
from .state import state

router = APIRouter(prefix="/__admin", tags=["admin"])


def _garde(request: Request) -> JSONResponse | None:
    if request.headers.get("X-Mock-Admin-Token") == settings.admin_token:
        return None
    return JSONResponse(status_code=401, content={"error": "invalid or missing X-Mock-Admin-Token"})


@router.post("/reset")
async def reset(request: Request) -> JSONResponse:
    if (refus := _garde(request)) is not None:
        return refus
    try:
        corps = await request.json()
    except ValueError:
        corps = {}
    seed = corps.get("seed") if isinstance(corps, dict) else None
    if seed is not None and not isinstance(seed, int):
        return JSONResponse(status_code=422, content={"error": "seed must be an integer"})
    state.reset(seed)
    return JSONResponse({"status": "reset", "seed": state.seed})


@router.get("/state")
def etat(request: Request) -> JSONResponse:
    if (refus := _garde(request)) is not None:
        return refus
    return JSONResponse(
        {
            "seed": state.seed,
            "property_id": settings.property_id,
            "clock_offset": horloge.offset_secondes,
            "virtual_now": virtual_now().isoformat(),
            "freshness_hours": settings.freshness_hours,
            "days_materialized": state.jours_materialises(),
            "request_counts_by_path": engine.request_counts,
            "last_request_by_path": engine.last_request_by_path,
            "injections": [regle.resume() for regle in engine.regles],
            "quota": {
                "tokens_per_day_remaining": settings.quota_tokens_per_day
                - state.quota_jour_consomme,
                "tokens_per_hour_remaining": settings.quota_tokens_per_hour
                - state.quota_heure_consomme,
            },
        }
    )


@router.post("/inject")
async def injecter(request: Request) -> JSONResponse:
    if (refus := _garde(request)) is not None:
        return refus
    try:
        corps = await request.json()
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "invalid JSON body"})
    if not isinstance(corps, dict) or corps.get("kind") not in KINDS:
        return JSONResponse(
            status_code=422,
            content={"error": f"kind must be one of {', '.join(KINDS)}"},
        )
    admis = {"scope", "status", "times", "seconds", "retry_after_seconds", "after_requests"}
    params: dict[str, Any] = {k: v for k, v in corps.items() if k in admis}
    regle = engine.ajouter(corps["kind"], **params)
    return JSONResponse(regle.resume())


@router.delete("/inject/{rule_id}")
def retirer(request: Request, rule_id: str) -> JSONResponse:
    if (refus := _garde(request)) is not None:
        return refus
    if not engine.retirer(rule_id):
        return JSONResponse(status_code=404, content={"error": f"unknown rule {rule_id}"})
    return JSONResponse({"status": "deleted", "rule_id": rule_id})


@router.post("/inject/clear")
def vider(request: Request) -> JSONResponse:
    if (refus := _garde(request)) is not None:
        return refus
    engine.vider()
    return JSONResponse({"status": "cleared"})


@router.post("/clock")
async def avancer_horloge(request: Request) -> JSONResponse:
    """Avance l'horloge VIRTUELLE — dates relatives, expiration des bearers et
    visibilité de fraîcheur vieillissent ensemble. Pas de retour en arrière :
    un monde qui rajeunit n'a aucun sens pour un consommateur incrémental."""
    if (refus := _garde(request)) is not None:
        return refus
    try:
        corps = await request.json()
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "invalid JSON body"})
    avance = corps.get("advance_seconds") if isinstance(corps, dict) else None
    if not isinstance(avance, int | float) or isinstance(avance, bool) or avance < 0:
        return JSONResponse(
            status_code=422,
            content={"error": "advance_seconds must be a non-negative number"},
        )
    horloge.offset_secondes += float(avance)
    return JSONResponse(
        {"clock_offset": horloge.offset_secondes, "virtual_now": virtual_now().isoformat()}
    )

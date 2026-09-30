"""Control plane /__admin — the mock's tooling, never a Google path.

Mounted at the ROOT (not under /v1beta) so a firewall can block it wholesale,
and ONLY when GA_MOCK_ADMIN_ENABLED=true: absent, not merely forbidden. It
exists because consumers run the mock in a CONTAINER (compose, CI sidecar):
without HTTP, there's no way left to reset state, advance the clock or
inject a failure between two tests.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .clock import clock, virtual_now
from .injection import KINDS, engine
from .settings import settings
from .state import state

router = APIRouter(prefix="/__admin", tags=["admin"])


def _guard(request: Request) -> JSONResponse | None:
    if request.headers.get("X-Mock-Admin-Token") == settings.admin_token:
        return None
    return JSONResponse(status_code=401, content={"error": "invalid or missing X-Mock-Admin-Token"})


@router.post("/reset")
async def reset(request: Request) -> JSONResponse:
    if (denial := _guard(request)) is not None:
        return denial
    try:
        body = await request.json()
    except ValueError:
        body = {}
    seed = body.get("seed") if isinstance(body, dict) else None
    if seed is not None and not isinstance(seed, int):
        return JSONResponse(status_code=422, content={"error": "seed must be an integer"})
    state.reset(seed)
    return JSONResponse({"status": "reset", "seed": state.seed})


@router.get("/state")
def get_state(request: Request) -> JSONResponse:
    if (denial := _guard(request)) is not None:
        return denial
    return JSONResponse(
        {
            "seed": state.seed,
            "property_id": settings.property_id,
            "clock_offset": clock.offset_seconds,
            "virtual_now": virtual_now().isoformat(),
            "freshness_hours": settings.freshness_hours,
            "days_materialized": state.days_materialized(),
            "request_counts_by_path": engine.request_counts,
            "last_request_by_path": engine.last_request_by_path,
            "injections": [rule.summary() for rule in engine.rules],
            "quota": {
                "tokens_per_day_remaining": settings.quota_tokens_per_day
                - state.quota_day_consumed,
                "tokens_per_hour_remaining": settings.quota_tokens_per_hour
                - state.quota_hour_consumed,
                "tokens_per_project_per_hour_remaining": (
                    settings.quota_tokens_per_project_per_hour - state.quota_project_hour_consumed
                ),
            },
        }
    )


@router.post("/inject")
async def inject(request: Request) -> JSONResponse:
    if (denial := _guard(request)) is not None:
        return denial
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "invalid JSON body"})
    if not isinstance(body, dict) or body.get("kind") not in KINDS:
        return JSONResponse(
            status_code=422,
            content={"error": f"kind must be one of {', '.join(KINDS)}"},
        )
    allowed = {"scope", "status", "times", "seconds", "retry_after_seconds", "after_requests"}
    params: dict[str, Any] = {k: v for k, v in body.items() if k in allowed}
    rule = engine.add(body["kind"], **params)
    return JSONResponse(rule.summary())


@router.delete("/inject/{rule_id}")
def remove_rule(request: Request, rule_id: str) -> JSONResponse:
    if (denial := _guard(request)) is not None:
        return denial
    if not engine.remove(rule_id):
        return JSONResponse(status_code=404, content={"error": f"unknown rule {rule_id}"})
    return JSONResponse({"status": "deleted", "rule_id": rule_id})


@router.post("/inject/clear")
def clear_injections(request: Request) -> JSONResponse:
    if (denial := _guard(request)) is not None:
        return denial
    engine.clear()
    return JSONResponse({"status": "cleared"})


@router.post("/clock")
async def advance_clock(request: Request) -> JSONResponse:
    """Advances the VIRTUAL clock — relative dates, bearer expiry and
    freshness visibility age together. No going back: a world that gets
    younger makes no sense to an incremental consumer."""
    if (denial := _guard(request)) is not None:
        return denial
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "invalid JSON body"})
    advance = body.get("advance_seconds") if isinstance(body, dict) else None
    if not isinstance(advance, int | float) or isinstance(advance, bool) or advance < 0:
        return JSONResponse(
            status_code=422,
            content={"error": "advance_seconds must be a non-negative number"},
        )
    clock.offset_seconds += float(advance)
    return JSONResponse(
        {"clock_offset": clock.offset_seconds, "virtual_now": virtual_now().isoformat()}
    )

"""Enveloppe d'erreur Google.

Toute erreur du vendeur — auth comprise, routes inconnues comprises — sort au
format google.rpc.Status sérialisé : `{"error": {"code", "message", "status"}}`.
Jamais le `{"detail": ...}` de FastAPI : reproduire l'enveloppe réelle est ce
qui permet au client d'insights360 d'écrire UN SEUL chemin d'erreur, exercé en
dev contre ce mock et inchangé en prod.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

# Correspondance HTTP → google.rpc.Code, restreinte aux codes que le mock émet.
# `METHOD_NOT_ALLOWED` n'existe PAS dans google.rpc : le comportement réel de
# la passerelle Google sur un mauvais verbe est invérifiable hors ligne — le
# choix est consigné dans docs/UNVERIFIED-FIELDS.md.
STATUTS: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}

# Valeur observée sur les API Google ; l'exactitude du realm est enregistrée
# comme non vérifiée, sa PRÉSENCE sur tout 401 est, elle, certaine.
WWW_AUTHENTICATE_401 = 'Bearer realm="https://accounts.google.com/", error="invalid_token"'

MESSAGE_401 = (
    "Request had invalid authentication credentials. Expected OAuth 2 access "
    "token, login cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)

MESSAGE_403_PROPRIETE = (
    "User does not have sufficient permissions for this property. To learn "
    "more about Property ID, see "
    "https://developers.google.com/analytics/devguides/reporting/data/v1/property-id."
)

MESSAGE_429_JOUR = (
    "Exhausted property tokens for a project per day. "
    "These quota tokens will return in less than 24 hours."
)

MESSAGE_429_HEURE = (
    "Exhausted property tokens for a project per hour. "
    "These quota tokens will return in under an hour."
)


class ErreurQuota(Exception):
    """Épuisement de quota → 429 RESOURCE_EXHAUSTED, wording façon Google."""


def erreur(
    code: int,
    message: str,
    *,
    statut: str | None = None,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Construit la réponse d'erreur au dialecte Google.

    `details` reste vide dans la plupart des cas : le vrai service y met des
    google.rpc.BadRequest/QuotaFailure que peu de clients lisent. On ne le
    peuple que quand on a un vrai contenu à y mettre, jamais pour décorer.
    """
    corps: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "status": statut or STATUTS.get(code, "UNKNOWN"),
        }
    }
    if details:
        corps["error"]["details"] = details
    entetes = dict(headers or {})
    if code == 401:
        entetes.setdefault("WWW-Authenticate", WWW_AUTHENTICATE_401)
    return JSONResponse(status_code=code, content=corps, headers=entetes)

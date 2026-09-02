"""Enveloppe d'erreur Google.

Les erreurs de la MÉTHODE sortent au format google.rpc.Status sérialisé :
`{"error": {"code", "message", "status"}}`. Jamais le `{"detail": ...}` de
FastAPI : reproduire l'enveloppe réelle est ce qui permet au client
d'insights360 d'écrire UN SEUL chemin d'erreur, exercé en dev contre ce mock
et inchangé en prod.

Les erreurs de ROUTAGE, elles, ne sortent pas du tout en JSON — relevé sur
analyticsdata.googleapis.com le 2026-09-02 : un chemin inconnu OU un mauvais
verbe rendent une page HTML 404 de la passerelle Google, `text/html`, pas
d'enveloppe, et jamais 405. Un consommateur qui fait `reponse.json()` sur ce
cas casse — en prod comme ici (cf. docs/CONFORMITE-REELLE.md).
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import HTMLResponse, JSONResponse

# Correspondance HTTP → google.rpc.Code, restreinte aux codes que le mock émet.
STATUTS: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}

# Deux 401 DISTINCTS, attestés : en-tête `Authorization` absent d'un côté,
# jeton présent mais refusé de l'autre. Le realm, le suffixe `error=` et les
# deux messages sont relevés tels quels — un client qui distingue « pas encore
# authentifié » de « jeton périmé » lit précisément ça.
WWW_AUTHENTICATE_401_MANQUANT = 'Bearer realm="https://accounts.google.com/"'
WWW_AUTHENTICATE_401_INVALIDE = 'Bearer realm="https://accounts.google.com/", error="invalid_token"'

MESSAGE_401_MANQUANT = (
    "Request is missing required authentication credential. Expected OAuth 2 "
    "access token, login cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)

MESSAGE_401_INVALIDE = (
    "Request had invalid authentication credentials. Expected OAuth 2 access "
    "token, login cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)


MESSAGE_403_PROPRIETE_INVALIDE = (
    "Invalid property ID: {id}. A numeric Property ID is required. To learn "
    "more about Property ID, see "
    "https://developers.google.com/analytics/devguides/reporting/data/v1/property-id."
)


# Le `details` que la couche de transcodage JSON joint à ses 400 : une
# violation par champ fautif, et le `message` est leur concaténation par
# retours à la ligne. Relevé tel quel — `field` (chemin PROTO, snake_case)
# n'accompagne `description` que quand la couche sait nommer le champ fautif :
# une valeur d'énumération invalide le sait, un nom de champ inconnu non.
def detail_bad_request(violations: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "@type": "type.googleapis.com/google.rpc.BadRequest",
            "fieldViolations": [
                {"field": champ, "description": description}
                if champ
                else {"description": description}
                for champ, description in violations
            ],
        }
    ]


def detail_credentials_manquantes(methode_rpc: str) -> list[dict[str, Any]]:
    """Le `details` que le vendeur joint AU SEUL 401 « credential manquant » —
    absent quand le jeton est présent mais refusé. `methode_rpc` est le nom
    gRPC complet de la méthode visée."""
    return [
        {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "CREDENTIALS_MISSING",
            "domain": "googleapis.com",
            "metadata": {
                "method": methode_rpc,
                "service": "analyticsdata.googleapis.com",
            },
        }
    ]


# La page de la passerelle est reproduite dans sa STRUCTURE (statut, type MIME,
# titre) et non au balisage près : c'est le fait qu'elle ne soit pas du JSON
# qui compte pour un consommateur, pas la feuille de style de Google.
_PAGE_404 = """<!DOCTYPE html>
<html lang=en>
  <meta charset=utf-8>
  <meta name=viewport content="initial-scale=1, minimum-scale=1, width=device-width">
  <title>Error 404 (Not Found)!!1</title>
  <p><b>404.</b> <ins>That's an error.</ins>
  <p>The requested URL <code>{chemin}</code> was not found on this server.
  <ins>That's all we know.</ins>
"""


def page_html_404(chemin: str) -> HTMLResponse:
    echappe = chemin.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        status_code=404,
        content=_PAGE_404.format(chemin=echappe),
        headers={"Content-Type": "text/html; charset=UTF-8"},
    )


MESSAGE_403_PROPRIETE = (
    "User does not have sufficient permissions for this property. To learn "
    "more about Property ID, see "
    "https://developers.google.com/analytics/devguides/reporting/data/v1/property-id."
)

MESSAGE_429_JOUR = (
    "Exhausted property tokens for a property per day. "
    "These quota tokens will return in less than 24 hours."
)

# Les trois seaux de jetons ont chacun leur message. Le libellé suit le NOM du
# seau côté vendeur (`tokensPerDay` et `tokensPerHour` sont des quotas de
# PROPRIÉTÉ, `tokensPerProjectPerHour` un quota de PROJET) — wording consigné
# comme non attesté : l'épuiser pour de vrai coûterait 24 h de quota réel.
MESSAGE_429_HEURE = (
    "Exhausted property tokens for a property per hour. "
    "These quota tokens will return in under an hour."
)

MESSAGE_429_PROJET_HEURE = (
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
        entetes.setdefault("WWW-Authenticate", WWW_AUTHENTICATE_401_INVALIDE)
    return JSONResponse(status_code=code, content=corps, headers=entetes)

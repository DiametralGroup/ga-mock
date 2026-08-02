"""Auth Google service-account, validée pour de vrai.

Deux moitiés :
  • `/token` — le flux JWT-bearer de oauth2.googleapis.com : l'assertion RS256
    est VÉRIFIÉE (structure, signature, iss, fenêtre temporelle, scope) contre
    la bi-clé factice committée. Un mock qui accepte n'importe quoi ne teste
    rien : un client qui signe mal doit échouer ICI, pas en prod.
  • les bearers — jetons opaques SANS ÉTAT (HMAC sur l'échéance + nonce),
    expirés selon l'horloge VIRTUELLE : /__admin/clock fait donc vieillir les
    jetons aussi, ce qui permet de tester le renouvellement côté client.

L'ordre des contrôles et les messages du vrai endpoint ne sont qu'en partie
attestés ; chaque wording incertain est consigné dans docs/UNVERIFIED-FIELDS.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import quote

from . import keypair
from .clock import virtual_now
from .rsa_min import b64url, b64url_decode, sign, verify
from .settings import BEARER_TTL_SECONDES, settings

GRANT_TYPE_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SCOPES_ACCEPTES = (
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics",
)
TOLERANCE_SECONDES = 60
DUREE_MAX_ASSERTION = 3600

_MESSAGE_FENETRE = (
    "Invalid JWT: Token must be a short-lived token (60 minutes) and in a "
    "reasonable timeframe. Check your iat and exp values in the JWT claim."
)


class ErreurToken(Exception):
    """Erreur du endpoint /token, au format OAuth2 (`error`/`error_description`).

    PAS l'enveloppe google.rpc : oauth2.googleapis.com parle RFC 6749, seule
    la surface Data API parle google.rpc.Status.
    """

    def __init__(self, code: str, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


def _cle_bearer() -> bytes:
    """Dérivée, pas tirée au sort : un redémarrage du mock ne doit pas
    invalider les bearers d'un test en cours — tout est fonction de la
    configuration, rien de l'instant du boot."""
    return hashlib.sha256(b"ga-mock:bearer:" + settings.sa_email.encode()).digest()


def _fenetre_valide(iat: int, exp: int, reference: int) -> bool:
    return not (
        iat > reference + TOLERANCE_SECONDES
        or exp < reference - TOLERANCE_SECONDES
        or exp <= iat
        or exp - iat > DUREE_MAX_ASSERTION + TOLERANCE_SECONDES
    )


def _segment_json(segment: str, contexte: str) -> dict[str, Any]:
    try:
        decode = json.loads(b64url_decode(segment))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ErreurToken("invalid_grant", f"Invalid JWT: unable to parse {contexte}.") from exc
    if not isinstance(decode, dict):
        raise ErreurToken("invalid_grant", f"Invalid JWT: {contexte} is not an object.")
    return decode


def valider_assertion(assertion: str) -> dict[str, Any]:
    """Contrôles dans l'ordre du vrai endpoint (tel qu'observé) : structure,
    signature, compte, fenêtre temporelle, scope. Retourne les claims."""
    morceaux = assertion.split(".")
    if len(morceaux) != 3 or not all(morceaux):
        raise ErreurToken("invalid_grant", "Invalid JWT: assertion must have 3 segments.")
    entete_b64, charge_b64, signature_b64 = morceaux
    entete = _segment_json(entete_b64, "token header")
    if entete.get("alg") != "RS256":
        raise ErreurToken(
            "invalid_grant", "Invalid JWT: only RS256 is supported for service accounts."
        )
    claims = _segment_json(charge_b64, "claims")
    try:
        signature = b64url_decode(signature_b64)
    except ValueError as exc:
        raise ErreurToken("invalid_grant", "Invalid JWT Signature.") from exc
    if not verify(f"{entete_b64}.{charge_b64}".encode(), signature, keypair.N, keypair.E):
        raise ErreurToken("invalid_grant", "Invalid JWT Signature.")
    if claims.get("iss") != settings.sa_email:
        raise ErreurToken("invalid_grant", "Invalid grant: account not found")
    iat, exp = claims.get("iat"), claims.get("exp")
    # DEUX horloges de référence, l'assertion doit être valide contre l'UNE :
    # un vrai client signe avec l'heure RÉELLE (août 2026 et au-delà), alors
    # que le monde du mock est ANCRÉ (juillet 2026) — exiger la seule horloge
    # virtuelle rejetterait tout client réel, exiger la seule horloge réelle
    # casserait les assertions fabriquées contre l'ancre (build_assertion).
    # Une assertion réellement périmée échoue contre LES DEUX. Affordance
    # consignée dans docs/UNVERIFIED-FIELDS.md (fenetre-assertion-double-horloge).
    if (
        not isinstance(iat, int)
        or not isinstance(exp, int)
        or not any(
            _fenetre_valide(iat, exp, reference)
            for reference in (int(virtual_now().timestamp()), int(time.time()))
        )
    ):
        raise ErreurToken("invalid_grant", _MESSAGE_FENETRE)
    scopes = str(claims.get("scope", "")).split()
    if not any(s in SCOPES_ACCEPTES for s in scopes):
        raise ErreurToken("invalid_scope", "Invalid OAuth scope or ID token audience provided.")
    # `aud` : tolérant par construction — derrière compose, l'assertion vise
    # http://ga-mock:8000/token pendant que le serveur se voit autrement.
    # Exiger l'égalité stricte casserait le cas nominal ; on vérifie la forme.
    aud = str(claims.get("aud", ""))
    if aud and not aud.rstrip("/").endswith("/token"):
        raise ErreurToken("invalid_grant", "Invalid JWT: aud must target the token endpoint.")
    return claims


def emettre_bearer() -> dict[str, Any]:
    echeance = int(virtual_now().timestamp()) + BEARER_TTL_SECONDES
    charge = b64url(json.dumps({"exp": echeance, "n": b64url(os.urandom(9))}).encode())
    mac = b64url(hmac.new(_cle_bearer(), charge.encode(), hashlib.sha256).digest())
    return {
        "access_token": f"ya29.mock.{charge}.{mac}",
        # 3599 et non 3600 : le vrai endpoint décompte la seconde d'émission.
        "expires_in": BEARER_TTL_SECONDES - 1,
        "token_type": "Bearer",
    }


def bearer_valide(token: str) -> bool:
    prefixe = "ya29.mock."
    if not token.startswith(prefixe):
        return False
    charge, separateur, mac = token[len(prefixe) :].partition(".")
    if not separateur or not charge or not mac:
        return False
    attendu = b64url(hmac.new(_cle_bearer(), charge.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(attendu, mac):
        return False
    try:
        claims = json.loads(b64url_decode(charge))
    except ValueError:
        return False
    exp = claims.get("exp")
    return isinstance(exp, int) and exp > int(virtual_now().timestamp())


def build_assertion(
    iss: str | None = None,
    scope: str | None = None,
    aud: str = "http://localhost:8013/token",
    iat: int | None = None,
    lifetime: int = 3600,
) -> str:
    """Signe une assertion avec la clé privée factice committée.

    Analogue du `build_client_jwt` de boondmanager-mock : les tests des
    consommateurs fabriquent leur jeton sans dépendance crypto. Les paramètres
    permettent aussi de fabriquer des assertions INVALIDES (mauvais iss, durée
    excessive…) pour tester les refus.
    """
    quand = int(virtual_now().timestamp()) if iat is None else iat
    entete = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    charge = b64url(
        json.dumps(
            {
                "iss": settings.sa_email if iss is None else iss,
                "scope": SCOPES_ACCEPTES[0] if scope is None else scope,
                "aud": aud,
                "iat": quand,
                "exp": quand + lifetime,
            }
        ).encode()
    )
    signature = b64url(sign(f"{entete}.{charge}".encode(), keypair.N, keypair.D))
    return f"{entete}.{charge}.{signature}"


def fixture_service_account(base_url: str) -> dict[str, str]:
    """Le JSON de service account standard, `token_uri` pointé sur CE serveur.

    Servi dynamiquement : committer une token_uri figée obligerait à deviner
    l'hôte de déploiement (localhost:8013 ? ga-mock:8000 ?). L'URL de la
    requête entrante le sait mieux que nous.
    """
    return {
        "type": "service_account",
        "project_id": "boreal-conseil-mock",
        "private_key_id": keypair.PRIVATE_KEY_ID,
        "private_key": keypair.PEM_PRIVE,
        "client_email": settings.sa_email,
        "client_id": "104727004242420010001",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": f"{base_url}/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            "https://www.googleapis.com/robot/v1/metadata/x509/" + quote(settings.sa_email, safe="")
        ),
        "universe_domain": "googleapis.com",
    }

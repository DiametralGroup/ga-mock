"""Google service-account auth, validated for real.

Two halves:
  • `/token` — the oauth2.googleapis.com JWT-bearer flow: the RS256 assertion
    is ACTUALLY VERIFIED (structure, signature, iss, time window, scope, aud)
    against the committed fake keypair. A mock that accepts anything tests
    nothing: a client that signs badly must fail HERE, not in prod.
  • bearers — STATELESS opaque tokens (HMAC over the expiry + nonce), expired
    according to the VIRTUAL clock: /__admin/clock therefore ages tokens too,
    which lets client-side renewal be tested.

The ORDER of checks and the wordings are now ATTESTED: they were recorded
against oauth2.googleapis.com on 2026-09-02 with a real service account
(cf. docs/CONFORMITE-REELLE.md, /token section). Two surprises reproduced
here because a consumer will meet them in prod:

  • a structurally undecodable assertion does NOT come out as `invalid_grant`
    with an explanatory message, but as `invalid_request` / plain "Bad
    Request" — the vendor doesn't say what's wrong;
  • a well-formed but unrecognized `scope` does NOT cause an error: the
    endpoint responds 200 with an `id_token` and NO `access_token`. A client
    doing `response["access_token"]` breaks on a KeyError, in prod as here.
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
from .settings import BEARER_TTL_SECONDS, settings

GRANT_TYPE_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ACCEPTED_SCOPES = (
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics",
)
TOLERANCE_SECONDS = 60
MAX_ASSERTION_DURATION = 3600
# Service account numeric identifier: `client_id` in the served JSON,
# `sub` in the id_token — the two MUST match, like at the vendor.
CLIENT_ID = "104727004242420010001"

# Wordings recorded FROM the vendor (2026-09-02). Do not "improve" them: a
# consumer may rely on them, and that's exactly what the mock exists to let
# them exercise.
_MESSAGE_BAD_REQUEST = "Bad Request"
_MESSAGE_SIGNATURE = "Invalid JWT Signature."
_MESSAGE_ACCOUNT = "Invalid grant: account not found"
_MESSAGE_IAT_MISSING = "Invalid JWT: iat (issued at) is not set."
_MESSAGE_EXP_MISSING = "Invalid JWT: exp (expiration time) is not set."
_MESSAGE_AUD = "Invalid JWT: Failed audience check."
_MESSAGE_SCOPE = "Invalid OAuth scope or ID token audience provided."
_MESSAGE_WINDOW = (
    "Invalid JWT: Token must be a short-lived token (60 minutes) and in a "
    "reasonable timeframe. Check your iat and exp values in the JWT claim."
)


class TokenError(Exception):
    """/token endpoint error, in OAuth2 format (`error`/`error_description`).

    NOT the google.rpc envelope: oauth2.googleapis.com speaks RFC 6749, only
    the Data API surface speaks google.rpc.Status.
    """

    def __init__(self, code: str, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


def _bearer_key() -> bytes:
    """Derived, not randomly drawn: restarting the mock must not invalidate
    the bearers of a test in progress — everything is a function of
    configuration, nothing of the boot instant."""
    return hashlib.sha256(b"ga-mock:bearer:" + settings.sa_email.encode()).digest()


def _window_valid(iat: int, exp: int, reference: int) -> bool:
    return not (
        iat > reference + TOLERANCE_SECONDS
        or exp < reference - TOLERANCE_SECONDS
        or exp <= iat
        or exp - iat > MAX_ASSERTION_DURATION + TOLERANCE_SECONDS
    )


def _json_segment(segment: str) -> dict[str, Any]:
    """An unreadable segment comes out as `invalid_request` / "Bad Request".

    Attested: the vendor does NOT say what's wrong in an undecodable
    assertion — neither the faulty segment nor the reason. Returning an
    explanatory message here would train the consumer for a diagnostic they
    will never get in prod.
    """
    try:
        decoded = json.loads(b64url_decode(segment))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("invalid_request", _MESSAGE_BAD_REQUEST) from exc
    if not isinstance(decoded, dict):
        raise TokenError("invalid_request", _MESSAGE_BAD_REQUEST)
    return decoded


def _jwt_int(value: Any) -> int | None:
    """`iat`/`exp` accepted as a number OR a digit string — attested: an
    assertion carrying `"iat": "1788361815"` gets a token from the vendor."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def validate_assertion(assertion: str) -> dict[str, Any]:
    """Checks in the ORDER of the real endpoint, recorded on 2026-09-02:
    structure → signature → account → iat/exp presence → window → non-empty
    scope → audience. Returns the claims."""
    parts = assertion.split(".")
    if len(parts) != 3 or not all(parts):
        raise TokenError("invalid_request", _MESSAGE_BAD_REQUEST)
    header_b64, payload_b64, signature_b64 = parts
    header = _json_segment(header_b64)
    claims = _json_segment(payload_b64)
    try:
        signature = b64url_decode(signature_b64)
    except ValueError as exc:
        raise TokenError("invalid_request", _MESSAGE_BAD_REQUEST) from exc
    # A non-RS256 `alg` (including `none` or absent) falls on the SAME
    # message as a bad signature: the vendor doesn't distinguish the two.
    if header.get("alg") != "RS256" or not verify(
        f"{header_b64}.{payload_b64}".encode(), signature, keypair.N, keypair.E
    ):
        raise TokenError("invalid_grant", _MESSAGE_SIGNATURE)
    if claims.get("iss") != settings.sa_email:
        raise TokenError("invalid_grant", _MESSAGE_ACCOUNT)
    iat, exp = _jwt_int(claims.get("iat")), _jwt_int(claims.get("exp"))
    if iat is None:
        raise TokenError("invalid_grant", _MESSAGE_IAT_MISSING)
    if exp is None:
        raise TokenError("invalid_grant", _MESSAGE_EXP_MISSING)
    # TWO reference clocks, the assertion must be valid against EITHER: a
    # real client signs with the REAL time (September 2026 and beyond),
    # while the mock's world is ANCHORED (July 2026) — requiring only the
    # virtual clock would reject every real client, requiring only the real
    # clock would break assertions manufactured against the anchor
    # (build_assertion). A genuinely expired assertion fails against BOTH.
    # Affordance recorded (fenetre-assertion-double-horloge).
    if not any(
        _window_valid(iat, exp, reference)
        for reference in (int(virtual_now().timestamp()), int(time.time()))
    ):
        raise TokenError("invalid_grant", _MESSAGE_WINDOW)
    # An EMPTY scope is an error; an unrecognized scope is not — it switches
    # the response to id_token (cf. token_response). Attested: wrong audience
    # + empty scope comes out as `invalid_scope`, so this check PRECEDES the
    # `aud` one.
    if not str(claims.get("scope", "")).split():
        raise TokenError("invalid_scope", _MESSAGE_SCOPE)
    # `aud`: tolerant by design — behind compose, the assertion targets
    # http://ga-mock:8000/token while the server sees itself differently. The
    # vendor requires strict equality; we check the SHAPE, with its message
    # (deliberate gap, recorded: aud-tolerant).
    aud = str(claims.get("aud", ""))
    if not aud or not aud.rstrip("/").endswith("/token"):
        raise TokenError("invalid_grant", _MESSAGE_AUD)
    return claims


def token_response(claims: dict[str, Any]) -> dict[str, Any]:
    """The 200 body: `access_token`… or `id_token` ALONE.

    Attested: as soon as ONE of the requested scopes isn't a recognized
    OAuth scope — even when another one is — the endpoint switches and
    returns an id_token without an access_token. Here "recognized" applies to
    Analytics scopes: the mock has no other universe to offer, and a consumer
    asking for something else won't get anything usable on this surface
    anyway.
    """
    scopes = str(claims.get("scope", "")).split()
    if all(s in ACCEPTED_SCOPES for s in scopes):
        return issue_bearer()
    return {"id_token": issue_id_token(claims)}


def issue_id_token(claims: dict[str, Any]) -> str:
    """Identity JWT signed with the fake keypair, claims modeled on those
    recorded at the vendor: `aud` carries the requested scope, `iss` stays
    accounts.google.com, `sub` is the account's numeric identifier."""
    now = int(virtual_now().timestamp())
    header = b64url(
        json.dumps({"alg": "RS256", "kid": keypair.PRIVATE_KEY_ID, "typ": "JWT"}).encode()
    )
    payload = b64url(
        json.dumps(
            {
                "aud": str(claims.get("scope", "")),
                "azp": settings.sa_email,
                "email": settings.sa_email,
                "email_verified": True,
                "exp": now + BEARER_TTL_SECONDS,
                "iat": now,
                "iss": "https://accounts.google.com",
                "sub": CLIENT_ID,
            }
        ).encode()
    )
    signature = b64url(sign(f"{header}.{payload}".encode(), keypair.N, keypair.D))
    return f"{header}.{payload}.{signature}"


def issue_bearer() -> dict[str, Any]:
    expiry = int(virtual_now().timestamp()) + BEARER_TTL_SECONDS
    payload = b64url(json.dumps({"exp": expiry, "n": b64url(os.urandom(9))}).encode())
    mac = b64url(hmac.new(_bearer_key(), payload.encode(), hashlib.sha256).digest())
    return {
        "access_token": f"ya29.mock.{payload}.{mac}",
        # 3599 not 3600: the real endpoint deducts the issuance second.
        "expires_in": BEARER_TTL_SECONDS - 1,
        "token_type": "Bearer",
    }


def bearer_valid(token: str) -> bool:
    prefix = "ya29.mock."
    if not token.startswith(prefix):
        return False
    payload, separator, mac = token[len(prefix) :].partition(".")
    if not separator or not payload or not mac:
        return False
    expected = b64url(hmac.new(_bearer_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, mac):
        return False
    try:
        claims = json.loads(b64url_decode(payload))
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
    """Signs an assertion with the committed fake private key.

    Analogous to boondmanager-mock's `build_client_jwt`: consumer tests build
    their token without a crypto dependency. The parameters also allow
    building INVALID assertions (bad iss, excessive duration…) to test
    rejections.
    """
    now = int(virtual_now().timestamp()) if iat is None else iat
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(
        json.dumps(
            {
                "iss": settings.sa_email if iss is None else iss,
                "scope": ACCEPTED_SCOPES[0] if scope is None else scope,
                "aud": aud,
                "iat": now,
                "exp": now + lifetime,
            }
        ).encode()
    )
    signature = b64url(sign(f"{header}.{payload}".encode(), keypair.N, keypair.D))
    return f"{header}.{payload}.{signature}"


def fixture_service_account(base_url: str) -> dict[str, str]:
    """The standard service account JSON, `token_uri` pointed at THIS server.

    Served dynamically: committing a fixed token_uri would force guessing the
    deployment host (localhost:8013? ga-mock:8000?). The incoming request's
    URL knows better than we do.
    """
    return {
        "type": "service_account",
        "project_id": "boreal-conseil-mock",
        "private_key_id": keypair.PRIVATE_KEY_ID,
        "private_key": keypair.PEM_PRIVATE_KEY,
        "client_email": settings.sa_email,
        "client_id": CLIENT_ID,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": f"{base_url}/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            "https://www.googleapis.com/robot/v1/metadata/x509/" + quote(settings.sa_email, safe="")
        ),
        "universe_domain": "googleapis.com",
    }

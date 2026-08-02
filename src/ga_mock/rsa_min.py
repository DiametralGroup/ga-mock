"""RSA minimal en stdlib : RS256 (RSASSA-PKCS1-v1_5 + SHA-256).

Pourquoi pas `cryptography` : les dépendances runtime du mock se limitent à
FastAPI + uvicorn, et la clé manipulée est LA NÔTRE (bi-clé factice committée,
cf. keypair.py). On n'a donc besoin ni de parsing PEM ni d'ASN.1 général : le
module reçoit (n, e, d) en entiers et fait l'arithmétique modulaire du
RFC 8017, rien d'autre. Ce n'est PAS une implémentation générale de RSA — hors
de ce mock, ne pas s'en servir.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

# DigestInfo DER pour SHA-256 (RFC 8017 §9.2, note 1) : l'« encodage ASN.1 »
# se réduit à préfixer ce blob CONSTANT au condensat — c'est ce qui rend la
# vérification faisable en stdlib sans bibliothèque ASN.1.
_PREFIXE_DIGESTINFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url(brut: bytes) -> str:
    """base64url SANS padding — un `=` final ferait échouer la vérification JWT."""
    return base64.urlsafe_b64encode(brut).rstrip(b"=").decode()


def b64url_decode(texte: str) -> bytes:
    manque = -len(texte) % 4
    return base64.urlsafe_b64decode(texte + "=" * manque)


def _emsa_pkcs1_v15(message: bytes, k: int) -> bytes:
    """EM = 0x00 0x01 FF…FF 0x00 ‖ DigestInfo(SHA-256(message)) — RFC 8017 §9.2."""
    t = _PREFIXE_DIGESTINFO_SHA256 + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise ValueError("module RSA trop court pour EMSA-PKCS1-v1_5")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def verify(message: bytes, signature: bytes, n: int, e: int) -> bool:
    """Compare l'ENCODAGE COMPLET, pas le seul condensat : c'est la parade
    classique aux signatures à padding malléable (Bleichenbacher '06)."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    return hmac.compare_digest(em, _emsa_pkcs1_v15(message, k))


def sign(message: bytes, n: int, d: int) -> bytes:
    """Signature avec la clé PRIVÉE factice — sert `build_assertion()` et les
    tests des consommateurs, jamais un usage réel."""
    k = (n.bit_length() + 7) // 8
    em = int.from_bytes(_emsa_pkcs1_v15(message, k), "big")
    return pow(em, d, n).to_bytes(k, "big")

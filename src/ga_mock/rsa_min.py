"""Minimal stdlib RSA: RS256 (RSASSA-PKCS1-v1_5 + SHA-256).

Why not `cryptography`: the mock's runtime dependencies are limited to
FastAPI + uvicorn, and the key handled is OUR OWN (fake committed keypair,
cf. keypair.py). So there's no need for PEM parsing or general ASN.1: the
module receives (n, e, d) as integers and does the modular arithmetic from
RFC 8017, nothing else. This is NOT a general-purpose RSA implementation —
do not use it outside this mock.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

# DigestInfo DER for SHA-256 (RFC 8017 §9.2, note 1): the "ASN.1 encoding"
# boils down to prefixing this CONSTANT blob to the digest — that's what
# makes verification feasible in stdlib without an ASN.1 library.
_DIGESTINFO_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url(raw: bytes) -> str:
    """base64url WITHOUT padding — a trailing `=` would fail JWT verification."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    missing = -len(text) % 4
    return base64.urlsafe_b64decode(text + "=" * missing)


def _emsa_pkcs1_v15(message: bytes, k: int) -> bytes:
    """EM = 0x00 0x01 FF…FF 0x00 ‖ DigestInfo(SHA-256(message)) — RFC 8017 §9.2."""
    t = _DIGESTINFO_SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise ValueError("RSA modulus too short for EMSA-PKCS1-v1_5")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def verify(message: bytes, signature: bytes, n: int, e: int) -> bool:
    """Compares the FULL ENCODING, not just the digest: the classic defense
    against malleable-padding signatures (Bleichenbacher '06)."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    return hmac.compare_digest(em, _emsa_pkcs1_v15(message, k))


def sign(message: bytes, n: int, d: int) -> bytes:
    """Signs with the fake PRIVATE key — used by `build_assertion()` and
    consumer tests, never for real use."""
    k = (n.bit_length() + 7) // 8
    em = int.from_bytes(_emsa_pkcs1_v15(message, k), "big")
    return pow(em, d, n).to_bytes(k, "big")

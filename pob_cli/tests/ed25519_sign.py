"""Test-only Ed25519 signing (RFC 8032, section 5.1.6) on top of the verifier's curve arithmetic.

pob only ever verifies; release.yml signs with OpenSSL. Tests sign with this so they need no
OpenSSL and stay deterministic. test_release_signature.py checks it against the RFC test vectors.
"""

from __future__ import annotations

import base64
import hashlib

from pob_cli.updater import signature as sig

_Point = tuple[int, int, int, int]


def _compress(p: _Point) -> bytes:
    zinv = pow(p[2], sig._P - 2, sig._P)
    x, y = p[0] * zinv % sig._P, p[1] * zinv % sig._P
    return int(y | ((x & 1) << 255)).to_bytes(32, "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(sig._mul(a, sig._G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(sig._mul(a, sig._G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % sig._L
    big_r = _compress(sig._mul(r, sig._G))
    k = int.from_bytes(hashlib.sha512(big_r + pub + message).digest(), "little") % sig._L
    return big_r + ((r + k * a) % sig._L).to_bytes(32, "little")


TEST_SEED = bytes(range(32))
TEST_PUBLIC_KEY = base64.b64encode(public_key(TEST_SEED)).decode()


def sign_sums_text(sums: bytes, seed: bytes = TEST_SEED) -> str:
    """Contents of a SHA256SUMS.txt.sig for `sums`, as release.yml writes it."""
    return base64.b64encode(sign(seed, sums)).decode() + "\n"

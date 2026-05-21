"""MeshCore identity key generation and public-key derivation."""

from __future__ import annotations

import secrets

_P = 2**255 - 19
_D = (-121665 * pow(121666, -1, _P)) % _P
_BASE_X = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BASE_Y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_IDENTITY = (0, 1)
_BASE = (_BASE_X, _BASE_Y)


def generate_private_key() -> str:
    """Generate a MeshCore-compatible 64-byte private key as lowercase hex."""
    scalar = bytearray(secrets.token_bytes(32))
    scalar[0] &= 248
    scalar[31] &= 63
    scalar[31] |= 64
    signing_component = secrets.token_bytes(32)
    return (bytes(scalar) + signing_component).hex()


def derive_public_key(private_key_hex: str) -> str:
    """Derive a MeshCore public key from a 64-byte private key.

    MeshCore private keys are not Ed25519 seeds. The first 32 bytes are the
    scalar used directly for scalar multiplication, and the remaining 32 bytes
    are used by signing code. Public key derivation therefore uses the first
    32 bytes as a little-endian scalar and does not hash the private key first.
    """
    private_key = bytes.fromhex(private_key_hex)
    if len(private_key) != 64:
        raise ValueError("MeshCore private key must be 64 bytes / 128 hex characters")
    scalar = int.from_bytes(private_key[:32], "little")
    return _encode_point(_scalar_mult(scalar, _BASE)).hex()


def _scalar_mult(scalar: int, point: tuple[int, int]) -> tuple[int, int]:
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _point_add(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = left
    x2, y2 = right
    x1x2 = x1 * x2 % _P
    y1y2 = y1 * y2 % _P
    dxxyy = _D * x1x2 * y1y2 % _P
    x3 = ((x1 * y2 + x2 * y1) * pow(1 + dxxyy, -1, _P)) % _P
    y3 = ((y1y2 + x1x2) * pow(1 - dxxyy, -1, _P)) % _P
    return x3, y3


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    encoded = bytearray(y.to_bytes(32, "little"))
    encoded[31] |= (x & 1) << 7
    return bytes(encoded)

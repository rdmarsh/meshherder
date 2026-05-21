"""Tests for MeshCore identity key helpers."""

import pytest

from mch.meshkey import derive_public_key, generate_private_key


def test_derive_public_key_matches_known_meshcore_vector():
    """MeshCore derives pubkey from the first 32 private-key bytes as a scalar."""
    private_key = (
        "489E11DCC0A5E037E65C90D2327AA11A42EAFE0C9F68DEBE82B0F71C88C0874B"
        "CC291D9B2B98A54F5C1426B7AB8156B0D684EAA4EBA755AC614A9FD32B74C308"
    )

    assert derive_public_key(private_key) == (
        "deadbeef02f4abe0528175198da136b6b202f58c0648fd8017a2196b675c7b55"
    )


def test_derive_public_key_rejects_non_64_byte_private_key():
    with pytest.raises(ValueError, match="64 bytes"):
        derive_public_key("00" * 32)


def test_generate_private_key_returns_clamped_64_byte_hex_key():
    private_key = generate_private_key()
    raw = bytes.fromhex(private_key)

    assert len(raw) == 64
    assert raw[0] & 0b111 == 0
    assert raw[31] & 0b10000000 == 0
    assert raw[31] & 0b01000000
    assert len(derive_public_key(private_key)) == 64

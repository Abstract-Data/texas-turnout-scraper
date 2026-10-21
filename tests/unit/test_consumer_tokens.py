"""Unit tests for consumer token hashing and credential extraction."""

from __future__ import annotations

import hashlib

import pytest

from texas_turnout_scraper import consumer_tokens as ct
from texas_turnout_scraper.consumer_tokens import (
    extract_consumer_credential,
    hash_token,
    kv_key_for_hash,
    mint_plaintext_token,
)


def test_hash_token_is_stable_hex() -> None:
    digest = hash_token("example-secret")
    assert len(digest) == 64
    assert digest == hash_token("example-secret")


def test_extract_bearer_and_api_key() -> None:
    assert extract_consumer_credential({"X-API-Key": "key-abc"}) == "key-abc"
    assert extract_consumer_credential({"Authorization": "Bearer tok-xyz"}) == "tok-xyz"
    assert extract_consumer_credential({"Authorization": "Basic x"}) is None
    assert extract_consumer_credential({}) is None


def test_kv_key_uses_prefix() -> None:
    digest = hash_token("x")
    assert kv_key_for_hash(digest).startswith("consumer_token:")


def test_mint_plaintext_has_entropy() -> None:
    a = mint_plaintext_token()
    b = mint_plaintext_token()
    assert a != b
    assert len(a) >= 32


def test_hash_token_pyscrypt_fallback_matches_hashlib() -> None:
    """Pyodide lacks OpenSSL hashlib.scrypt; fallback must match CLI digests."""
    reference = hashlib.scrypt(
        b"example-secret",
        salt=ct._CONSUMER_TOKEN_PEPPER,
        n=ct._SCRYPT_N,
        r=ct._SCRYPT_R,
        p=ct._SCRYPT_P,
        dklen=ct._SCRYPT_DKLEN,
    ).hex()

    def _no_scrypt(*_args: object, **_kwargs: object) -> bytes:
        raise AttributeError("hashlib.scrypt unavailable")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ct.hashlib, "scrypt", _no_scrypt, raising=False)
    try:
        assert ct.hash_token("example-secret") == reference
    finally:
        monkeypatch.undo()

"""Unit tests for consumer token hashing and credential extraction."""

from __future__ import annotations

import hashlib
import hmac

from texas_turnout_scraper import consumer_tokens as ct
from texas_turnout_scraper.consumer_tokens import (
    DIGEST_SCHEME_V2,
    extract_consumer_credential,
    hash_token,
    kv_key_for_hash,
    mint_plaintext_token,
)


def test_hash_token_v2_is_stable_hmac_sha256() -> None:
    digest = hash_token("example-secret")
    assert digest.startswith(f"{DIGEST_SCHEME_V2}:")
    hex_part = digest.split(":", 1)[1]
    assert len(hex_part) == 64
    expected = hmac.new(
        ct._CONSUMER_TOKEN_PEPPER_V2,
        b"example-secret",
        hashlib.sha256,
    ).hexdigest()
    assert hex_part == expected
    assert digest == hash_token("example-secret")


def test_hash_token_v2_prefixed_not_legacy_scrypt_shape() -> None:
    """Pre-v2 scrypt rows used bare 64-char hex; v2 digests are ``v2:<hex>``."""
    digest = hash_token("legacy-check")
    assert digest.startswith(f"{DIGEST_SCHEME_V2}:")
    assert len(digest) > 64


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

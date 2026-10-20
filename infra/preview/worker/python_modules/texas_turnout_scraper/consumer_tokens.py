"""Minted consumer API tokens for the preview early-vote read API.

Plaintext tokens are shown once at mint time. Only SHA-256 digests are stored in
D1 and KV. Validation accepts ``Authorization: Bearer`` or ``X-API-Key``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from collections.abc import Mapping

AUTHORIZATION_HEADER = "Authorization"
X_API_KEY_HEADER = "X-API-Key"
BEARER_PREFIX = "bearer "

KV_PREFIX = "consumer_token:"

_SCHEMA_CREATES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS consumer_api_token (
  token_id TEXT PRIMARY KEY NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  revoked_at TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_consumer_api_token_hash ON consumer_api_token(token_hash)",
)

INSERT_SQL = (
    "INSERT INTO consumer_api_token (token_id, token_hash, label, created_at) VALUES (?, ?, ?, ?)"
)
LIST_SQL = (
    "SELECT token_id, label, created_at, revoked_at "
    "FROM consumer_api_token ORDER BY created_at DESC"
)
REVOKE_SQL = (
    "UPDATE consumer_api_token SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL"
)
ACTIVE_BY_HASH_SQL = (
    "SELECT token_id FROM consumer_api_token WHERE token_hash = ? AND revoked_at IS NULL LIMIT 1"
)


def schema_creates() -> tuple[str, ...]:
    """DDL applied on first admin use (idempotent)."""
    return _SCHEMA_CREATES


def mint_plaintext_token() -> str:
    """Return a high-entropy secret suitable for a single consumer."""
    return secrets.token_urlsafe(32)


def new_token_id() -> str:
    return secrets.token_hex(16)


def hash_token(plaintext: str) -> str:
    """Stable digest stored in D1/KV; never log ``plaintext``."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def kv_key_for_hash(token_hash: str) -> str:
    return f"{KV_PREFIX}{token_hash}"


def utc_now_iso() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def _header(headers: Mapping[str, str], name: str) -> str:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value.strip()
    return ""


def extract_consumer_credential(headers: Mapping[str, str]) -> str | None:
    """Parse Bearer or X-API-Key; returns raw secret or None."""
    api_key = _header(headers, X_API_KEY_HEADER)
    if api_key:
        return api_key
    auth = _header(headers, AUTHORIZATION_HEADER)
    if not auth:
        return None
    if auth.lower().startswith(BEARER_PREFIX):
        token = auth[len(BEARER_PREFIX) :].strip()
        return token or None
    return None

"""Minted consumer API tokens for the preview early-vote read API.

Plaintext tokens are shown once at mint time. Only digests are stored in D1 and
KV (scheme ``v2`` = HMAC-SHA256). Validation accepts ``Authorization: Bearer`` or
``X-API-Key``. Legacy scrypt digests (preview-only, pre-v2) no longer match and
fail closed with 401 until tokens are re-minted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from collections.abc import Mapping

AUTHORIZATION_HEADER = "Authorization"
X_API_KEY_HEADER = "X-API-Key"
BEARER_PREFIX = "bearer "

KV_PREFIX = "consumer_token:"

# aggregates: counts only (every token minted before scopes existed). roster: row-level access.
SCOPE_AGGREGATES = "aggregates"
SCOPE_ROSTER = "roster"
SCOPES = (SCOPE_AGGREGATES, SCOPE_ROSTER)

# Active digest scheme (fast on Pyodide; shared by CLI mint and Worker auth).
DIGEST_SCHEME_V2 = "v2"
_CONSUMER_TOKEN_PEPPER_V2 = b"tx-turnout-preview-consumer-v2-hmac-pepper!!"

_SCHEMA_CREATES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS consumer_api_token (
  token_id TEXT PRIMARY KEY NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  scope TEXT NOT NULL DEFAULT 'aggregates'
)""",
    "CREATE INDEX IF NOT EXISTS idx_consumer_api_token_hash ON consumer_api_token(token_hash)",
)

SCOPE_COLUMNS_SQL = "SELECT name FROM pragma_table_info('consumer_api_token')"
ADD_SCOPE_COLUMN_SQL = (
    "ALTER TABLE consumer_api_token ADD COLUMN scope TEXT NOT NULL DEFAULT 'aggregates'"
)
INSERT_SQL = (
    "INSERT INTO consumer_api_token (token_id, token_hash, label, created_at, scope) "
    "VALUES (?, ?, ?, ?, ?)"
)
LIST_SQL = (
    "SELECT token_id, label, created_at, revoked_at, scope "
    "FROM consumer_api_token ORDER BY created_at DESC"
)
REVOKE_SQL = (
    "UPDATE consumer_api_token SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL"
)
# SELECT * so the lookup still works on a table that has not been migrated to add scope.
ACTIVE_BY_HASH_SQL = (
    "SELECT * FROM consumer_api_token WHERE token_hash = ? AND revoked_at IS NULL LIMIT 1"
)


def schema_creates() -> tuple[str, ...]:
    """DDL applied on first admin use (idempotent)."""
    return _SCHEMA_CREATES


def mint_plaintext_token() -> str:
    """Return a high-entropy secret suitable for a single consumer."""
    return secrets.token_urlsafe(32)


def new_token_id() -> str:
    return secrets.token_hex(16)


def hash_token(api_token: str) -> str:
    """Stable digest stored in D1/KV; never log the minted token."""
    digest = hmac.new(
        _CONSUMER_TOKEN_PEPPER_V2,
        api_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{DIGEST_SCHEME_V2}:{digest}"


def parse_scope(value: str) -> str:
    if value not in SCOPES:
        msg = f"scope must be one of: {', '.join(SCOPES)}"
        raise ValueError(msg)
    return value


def kv_value_for_scope(scope: str) -> str:
    return parse_scope(scope)


def scope_from_kv_value(value: str) -> str:
    """Map a KV cache value to a scope; the legacy value ``1`` and unknowns mean aggregates."""
    return SCOPE_ROSTER if value.strip() == SCOPE_ROSTER else SCOPE_AGGREGATES


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

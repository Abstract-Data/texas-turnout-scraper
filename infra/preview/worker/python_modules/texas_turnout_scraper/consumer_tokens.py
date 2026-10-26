"""Minted consumer API tokens for the preview early-vote read API.

Plaintext tokens are shown once at issue time. Only digests are stored in D1 and
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
  scope TEXT NOT NULL DEFAULT 'aggregates',
  expires_at TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_consumer_api_token_hash ON consumer_api_token(token_hash)",
)

SCOPE_COLUMNS_SQL = "SELECT name FROM pragma_table_info('consumer_api_token')"
ADD_SCOPE_COLUMN_SQL = (
    "ALTER TABLE consumer_api_token ADD COLUMN scope TEXT NOT NULL DEFAULT 'aggregates'"
)
ADD_EXPIRES_COLUMN_SQL = "ALTER TABLE consumer_api_token ADD COLUMN expires_at TEXT"
INSERT_SQL = (
    "INSERT INTO consumer_api_token "
    "(token_id, token_hash, label, created_at, scope, expires_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
LIST_SQL = (
    "SELECT token_id, label, created_at, revoked_at, scope, expires_at "
    "FROM consumer_api_token ORDER BY created_at DESC"
)
SELECT_BY_ID_SQL = (
    "SELECT token_hash, scope, revoked_at, expires_at FROM consumer_api_token "
    "WHERE token_id = ? LIMIT 1"
)
UPDATE_EXPIRY_SQL = "UPDATE consumer_api_token SET expires_at = ? WHERE token_id = ?"
REVOKE_SQL = (
    "UPDATE consumer_api_token SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL"
)
# SELECT * so the lookup still works on a table that has not been migrated to add scope or
# expires_at. Expiry is checked in Python (``is_expired``), not in SQL.
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


# KV rejects an expiration TTL under 60 seconds.
KV_MIN_TTL_SECONDS = 60
DEFAULT_KEY_DAYS = 30
MAX_KEY_DAYS = 3650


def _parse_instant(value: str) -> dt.datetime | None:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def is_expired(expires_at: object, now: dt.datetime) -> bool:
    """True when ``expires_at`` is at or before ``now``.

    ``None`` and the empty string mean the token never expires (every token minted before
    expiry existed). Anything else that is not an ISO-8601 instant fails closed as expired.
    """
    if expires_at is None or expires_at == "":
        return False
    if not isinstance(expires_at, str):
        return True
    parsed = _parse_instant(expires_at)
    return parsed is None or parsed <= now


def expiry_iso(now: dt.datetime, days: int) -> str:
    return (now + dt.timedelta(days=days)).replace(microsecond=0).isoformat()


def extended_expiry(current: object, now: dt.datetime, days: int) -> str:
    """New expiry ``max(now, current) + days``; a missing or malformed ``current`` is now."""
    base = now
    if isinstance(current, str) and current:
        parsed = _parse_instant(current)
        if parsed is not None and parsed > now:
            base = parsed
    return expiry_iso(base, days)


def kv_ttl_seconds(expires_at: str | None, now: dt.datetime) -> int | None:
    """KV ``expiration_ttl`` for an expiry, or None for a token that never expires."""
    if expires_at is None or expires_at == "":
        return None
    parsed = _parse_instant(expires_at)
    if parsed is None:
        return KV_MIN_TTL_SECONDS
    return max(KV_MIN_TTL_SECONDS, int((parsed - now).total_seconds()))


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

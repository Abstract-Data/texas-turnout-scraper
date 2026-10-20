"""Preview read API authentication: Cloudflare Access and minted consumer tokens.

Access service tokens use ``CF-Access-Client-Id`` / ``CF-Access-Client-Secret``.
Consumers may send ``Authorization: Bearer`` or ``X-API-Key``. Only digests are
stored; plaintext tokens are never logged.

https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any

from ev_api.read import KvNamespace
from texas_turnout_scraper.civix.read_client import (
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_CLIENT_SECRET_HEADER,
)
from texas_turnout_scraper.consumer_tokens import (
    ACTIVE_BY_HASH_SQL,
    extract_consumer_credential,
    hash_token,
    kv_key_for_hash,
)
from texas_turnout_scraper.early_vote_d1_read import D1Database


def _header(headers: Mapping[str, str], name: str) -> str:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value
    return ""


def access_authorized(
    headers: Mapping[str, str],
    *,
    client_id: str,
    client_secret: str,
) -> bool:
    """True only when both configured credentials match. Empty config is closed."""
    if client_id == "" or client_secret == "":
        return False
    presented_id = _header(headers, CF_ACCESS_CLIENT_ID_HEADER)
    presented_secret = _header(headers, CF_ACCESS_CLIENT_SECRET_HEADER)
    id_ok = hmac.compare_digest(presented_id, client_id)
    secret_ok = hmac.compare_digest(presented_secret, client_secret)
    return id_ok and secret_ok


def _row_value(row: object, key: str) -> object | None:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


async def _token_active_in_d1(db: D1Database, digest: str) -> bool:
    row = await db.prepare(ACTIVE_BY_HASH_SQL).bind(digest).first()
    if row is None:
        return False
    token_id = _row_value(row, "token_id")
    return isinstance(token_id, str) and bool(token_id)


async def consumer_token_authorized(
    headers: Mapping[str, str],
    *,
    kv: KvNamespace,
    token_db: Any | None,
) -> bool:
    """True when a minted consumer credential matches KV or preview D1."""
    raw = extract_consumer_credential(headers)
    if not raw:
        return False
    digest = hash_token(raw)
    cached = await kv.get(kv_key_for_hash(digest))
    if cached is not None and cached.strip():
        return True
    if token_db is None:
        return False
    return await _token_active_in_d1(token_db, digest)


async def read_request_authorized(
    headers: Mapping[str, str],
    *,
    access_client_id: str,
    access_client_secret: str,
    kv: KvNamespace,
    token_db: Any | None,
) -> bool:
    """Dual auth: valid Access service token OR active consumer bearer/API key."""
    if access_authorized(
        headers,
        client_id=access_client_id,
        client_secret=access_client_secret,
    ):
        return True
    return await consumer_token_authorized(headers, kv=kv, token_db=token_db)

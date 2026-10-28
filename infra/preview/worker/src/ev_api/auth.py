"""Preview read API authentication: minted consumer tokens, checked by one route dependency.

Consumers send ``Authorization: Bearer`` or ``X-API-Key``. Only digests are stored; plaintext
tokens are never logged. The Cloudflare Access service token is enforced at the edge, and the
Worker does not accept it as a credential: every consumer holds the same pair, so only the
minted token can be revoked per consumer.

The app applies :func:`scope_dependency` for aggregates scope to every route, and roster routes
add one for roster scope, so a new route is authenticated without its handler doing anything.

A consumer token whose ``expires_at`` is set and at or before now is rejected on the D1 path.
Its KV entry carries an expiration TTL, so a KV hit is bounded by it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ev_api.read import KvNamespace
from texas_turnout_scraper.consumer_tokens import (
    ACTIVE_BY_HASH_SQL,
    SCOPE_AGGREGATES,
    extract_consumer_credential,
    hash_token,
    is_expired,
    kv_key_for_hash,
    scope_allows,
    scope_from_stored,
)
from texas_turnout_scraper.early_vote_d1_read import D1Database

# Request state attribute holding the looked-up scope, so stacked dependencies look it up once.
_SCOPE_STATE = "consumer_scope"
_UNSET = object()


class AuthDenied(Exception):
    """Raised by the auth dependency; the app turns it into ``response``."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status_code)
        self.response = response


def _row_value(row: object, key: str) -> object | None:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


async def _token_scope_in_d1(db: D1Database, digest: str) -> str | None:
    row = await db.prepare(ACTIVE_BY_HASH_SQL).bind(digest).first()
    if row is None:
        return None
    token_id = _row_value(row, "token_id")
    if not (isinstance(token_id, str) and token_id):
        return None
    if is_expired(_row_value(row, "expires_at"), dt.datetime.now(tz=dt.timezone.utc)):
        return None
    scope = _row_value(row, "scope")
    # A table not yet migrated has no scope column; every token in it predates roster scope.
    return SCOPE_AGGREGATES if scope is None else scope_from_stored(scope)


async def consumer_token_scope(
    headers: Mapping[str, str],
    *,
    kv: KvNamespace,
    token_db: Any | None,
) -> str | None:
    """Scope of the minted consumer credential, or None when it is missing, inactive or unknown."""
    raw = extract_consumer_credential(headers)
    if not raw:
        return None
    digest = hash_token(raw)
    cached = await kv.get(kv_key_for_hash(digest))
    if cached is not None and cached.strip():
        return scope_from_stored(cached)
    if token_db is None:
        return None
    return await _token_scope_in_d1(token_db, digest)


def scope_dependency(
    deps_for_request: Callable[[Request], Any],
    required: str,
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: 401 without an active token, 403 when its scope is below ``required``."""

    async def require_scope(request: Request) -> None:
        scope = getattr(request.state, _SCOPE_STATE, _UNSET)
        if scope is _UNSET:
            active = deps_for_request(request)
            scope = await consumer_token_scope(
                request.headers, kv=active.kv, token_db=active.token_db
            )
            setattr(request.state, _SCOPE_STATE, scope)
        if not isinstance(scope, str):
            raise AuthDenied(Response(status_code=401))
        if not scope_allows(scope, required):
            raise AuthDenied(JSONResponse({"detail": "forbidden"}, status_code=403))

    return require_scope

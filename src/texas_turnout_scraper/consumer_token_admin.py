"""Manage consumer API tokens in preview D1 + KV via the Cloudflare REST API."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from texas_turnout_scraper.consumer_tokens import (
    ADD_EXPIRES_COLUMN_SQL,
    ADD_SCOPE_COLUMN_SQL,
    INSERT_SQL,
    LIST_SQL,
    REVOKE_SQL,
    SCOPE_AGGREGATES,
    SCOPE_COLUMNS_SQL,
    SELECT_BY_ID_SQL,
    UPDATE_EXPIRY_SQL,
    expiry_iso,
    extended_expiry,
    hash_token,
    is_expired,
    kv_key_for_hash,
    kv_ttl_seconds,
    kv_value_for_scope,
    mint_plaintext_token,
    new_token_id,
    parse_scope,
    schema_creates,
    utc_now_iso,
)
from texas_turnout_scraper.early_vote_sync import EarlyVoteSyncError

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


class TokenNotFoundError(LookupError):
    """No consumer token has the given id."""


class TokenNoExpiryError(Exception):
    """The token never expires; extending it would put an expiry on it."""


class TokenRevokedError(ValueError):
    """The token is revoked and cannot be extended."""


@dataclass(frozen=True)
class ConsumerTokenRecord:
    token_id: str
    label: str
    created_at: str
    revoked_at: str | None
    scope: str = SCOPE_AGGREGATES
    expires_at: str | None = None

    def status(self, now: dt.datetime) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "expired" if is_expired(self.expires_at, now) else "active"


@dataclass(frozen=True)
class IssuedToken:
    """A freshly generated key; ``plaintext`` goes only to 1Password and an opt-in print."""

    token_id: str
    plaintext: str
    digest: str
    label: str
    scope: str
    created_at: str
    expires_at: str


class ConsumerTokenAdmin:
    """Issue, extend, list, and revoke hashed consumer tokens."""

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        d1_database_id: str,
        kv_namespace_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not account_id or not api_token or not d1_database_id or not kv_namespace_id:
            raise EarlyVoteSyncError(
                "Cloudflare account id, API token, preview D1 database id, "
                "and KV namespace id are required"
            )
        self._account_id = account_id
        self._d1_database_id = d1_database_id
        self._namespace_id = kv_namespace_id
        self._owns_client = client is None
        self._schema_ready = False
        self._client = client or httpx.Client(
            base_url=CLOUDFLARE_API_BASE,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @classmethod
    def from_env(cls) -> ConsumerTokenAdmin:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
        d1_id = os.environ.get("CLOUDFLARE_PREVIEW_D1_DATABASE_ID", "").strip()
        kv_id = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID", "").strip()
        return cls(
            account_id=account_id,
            api_token=api_token,
            d1_database_id=d1_id,
            kv_namespace_id=kv_id,
        )

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self._statements([(sql, []) for sql in schema_creates()])
        columns = {row.get("name") for row in self._query_rows(SCOPE_COLUMNS_SQL, [])}
        if "scope" not in columns:
            self._statements([(ADD_SCOPE_COLUMN_SQL, [])])
        if "expires_at" not in columns:
            self._statements([(ADD_EXPIRES_COLUMN_SQL, [])])
        self._schema_ready = True

    @staticmethod
    def issue(
        label: str,
        *,
        scope: str = SCOPE_AGGREGATES,
        days: int,
        now: dt.datetime | None = None,
    ) -> IssuedToken:
        """Generate a key locally. Nothing is written until :meth:`store`."""
        scope = parse_scope(scope)
        moment = now or dt.datetime.now(tz=dt.timezone.utc)
        plaintext = mint_plaintext_token()
        return IssuedToken(
            token_id=new_token_id(),
            plaintext=plaintext,
            digest=hash_token(plaintext),
            label=label,
            scope=scope,
            created_at=moment.replace(microsecond=0).isoformat(),
            expires_at=expiry_iso(moment, days),
        )

    def store(self, issued: IssuedToken, *, now: dt.datetime | None = None) -> None:
        """Write the D1 row, then the KV entry with a TTL matching the expiry."""
        self.ensure_schema()
        self._statements(
            [
                (
                    INSERT_SQL,
                    [
                        issued.token_id,
                        issued.digest,
                        issued.label,
                        issued.created_at,
                        issued.scope,
                        issued.expires_at,
                    ],
                )
            ]
        )
        moment = now or dt.datetime.now(tz=dt.timezone.utc)
        self._kv_put(
            kv_key_for_hash(issued.digest),
            kv_value_for_scope(issued.scope),
            expiration_ttl=kv_ttl_seconds(issued.expires_at, moment),
        )

    def extend(
        self,
        token_id: str,
        days: int,
        *,
        set_expiry: bool = False,
        now: dt.datetime | None = None,
    ) -> str:
        """Push the expiry to ``max(now, current) + days``; returns the new ``expires_at``."""
        self.ensure_schema()
        rows = self._query_rows(SELECT_BY_ID_SQL, [token_id])
        if not rows:
            raise TokenNotFoundError("unknown token_id")
        row = rows[0]
        if row.get("revoked_at"):
            raise TokenRevokedError("token is revoked")
        digest = row.get("token_hash")
        if not isinstance(digest, str) or not digest:
            raise TokenNotFoundError("unknown token_id")
        if not row.get("expires_at") and not set_expiry:
            raise TokenNoExpiryError("token has no expiry")
        scope = row.get("scope")
        moment = now or dt.datetime.now(tz=dt.timezone.utc)
        new_expiry = extended_expiry(row.get("expires_at"), moment, days)
        self._statements([(UPDATE_EXPIRY_SQL, [new_expiry, token_id])])
        self._kv_put(
            kv_key_for_hash(digest),
            kv_value_for_scope(scope if isinstance(scope, str) and scope else SCOPE_AGGREGATES),
            expiration_ttl=kv_ttl_seconds(new_expiry, moment),
        )
        return new_expiry

    def list_tokens(self) -> list[ConsumerTokenRecord]:
        self.ensure_schema()
        rows = self._query_rows(LIST_SQL, [])
        records: list[ConsumerTokenRecord] = []
        for row in rows:
            token_id = row.get("token_id")
            label = row.get("label")
            created_at = row.get("created_at")
            revoked_at = row.get("revoked_at")
            if not isinstance(token_id, str) or not isinstance(label, str):
                continue
            if not isinstance(created_at, str):
                continue
            revoked = revoked_at if isinstance(revoked_at, str) and revoked_at else None
            raw_scope = row.get("scope")
            raw_expires = row.get("expires_at")
            records.append(
                ConsumerTokenRecord(
                    token_id=token_id,
                    label=label,
                    created_at=created_at,
                    revoked_at=revoked,
                    scope=raw_scope
                    if isinstance(raw_scope, str) and raw_scope
                    else SCOPE_AGGREGATES,
                    expires_at=raw_expires
                    if isinstance(raw_expires, str) and raw_expires
                    else None,
                )
            )
        return records

    def revoke(self, token_id: str) -> bool:
        self.ensure_schema()
        rows = self._query_rows(
            "SELECT token_hash FROM consumer_api_token WHERE token_id = ? LIMIT 1",
            [token_id],
        )
        if not rows:
            return False
        digest = rows[0].get("token_hash")
        if not isinstance(digest, str) or not digest:
            return False
        when = utc_now_iso()
        self._statements([(REVOKE_SQL, [when, token_id])])
        self._kv_delete(kv_key_for_hash(digest))
        return True

    def _query_rows(self, sql: str, params: list[str]) -> list[dict[str, Any]]:
        payload = self._api(
            "POST",
            f"/accounts/{self._account_id}/d1/database/{self._d1_database_id}/query",
            json={"batch": [{"sql": sql, "params": params}]},
        )
        _require_query_success(payload)
        return _first_result_rows(payload)

    def _statements(self, statements: list[tuple[str, list[str]]]) -> None:
        if not statements:
            return
        body = {"batch": [{"sql": sql, "params": params} for sql, params in statements]}
        payload = self._api(
            "POST",
            f"/accounts/{self._account_id}/d1/database/{self._d1_database_id}/query",
            json=body,
        )
        _require_query_success(payload)

    def _kv_put(self, key: str, value: str, *, expiration_ttl: int | None = None) -> None:
        path = _kv_value_path(self._account_id, self._namespace_id, key)
        params = {"expiration_ttl": expiration_ttl} if expiration_ttl is not None else None
        response = self._client.put(
            path,
            content=value.encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
            params=params,
        )
        if response.status_code >= 400:
            raise EarlyVoteSyncError(f"Cloudflare KV write failed with HTTP {response.status_code}")

    def _kv_delete(self, key: str) -> None:
        path = _kv_value_path(self._account_id, self._namespace_id, key)
        response = self._client.delete(path)
        if response.status_code not in {200, 404}:
            raise EarlyVoteSyncError(
                f"Cloudflare KV delete failed with HTTP {response.status_code}"
            )

    def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise EarlyVoteSyncError(f"Cloudflare HTTP {response.status_code} for {method} {path}")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise EarlyVoteSyncError(f"Cloudflare API error for {method}")
        return payload


def _kv_value_path(account_id: str, namespace_id: str, key: str) -> str:
    encoded = quote(key, safe="")
    return f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{encoded}"


def _require_query_success(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise EarlyVoteSyncError("D1 query response was not a JSON object")
    result = payload.get("result")
    if not isinstance(result, list):
        raise EarlyVoteSyncError("D1 query response did not include a result list")
    for item in result:
        if not isinstance(item, dict):
            continue
        if item.get("success") is False:
            raise EarlyVoteSyncError("D1 query batch reported failure")


def _first_result_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if not isinstance(result, list) or not result:
        return []
    first = result[0]
    if not isinstance(first, dict):
        return []
    rows = first.get("results")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]

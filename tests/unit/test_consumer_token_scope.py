"""Token scopes: aggregates (default, all existing tokens) and roster (row-level access)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from ev_api.auth import consumer_token_scope

from texas_turnout_scraper.consumer_token_admin import ConsumerTokenAdmin
from texas_turnout_scraper.consumer_tokens import (
    SCOPE_AGGREGATES,
    SCOPE_ROSTER,
    hash_token,
    kv_key_for_hash,
    kv_value_for_scope,
    parse_scope,
    scope_allows,
    scope_from_stored,
)

ACCOUNT, D1_ID, KV_ID = "acct", "db-uuid", "kv-ns"


class _Kv:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _Db:
    """Stands in for the D1 token table; rows keyed by token hash."""

    def __init__(self, rows: dict[str, dict[str, str]]) -> None:
        self._rows = rows
        self._hash = ""

    def prepare(self, sql: str) -> _Db:
        return self

    def bind(self, digest: str) -> _Db:
        self._hash = digest
        return self

    async def first(self) -> object | None:
        return self._rows.get(self._hash)


def test_parse_scope() -> None:
    assert parse_scope("roster") == SCOPE_ROSTER
    assert parse_scope("aggregates") == SCOPE_AGGREGATES
    with pytest.raises(ValueError):
        parse_scope("admin")


def test_kv_values_round_trip_and_legacy_one_is_aggregates() -> None:
    assert scope_from_stored(kv_value_for_scope(SCOPE_ROSTER)) == SCOPE_ROSTER
    assert scope_from_stored(kv_value_for_scope(SCOPE_AGGREGATES)) == SCOPE_AGGREGATES
    assert scope_from_stored("1") == SCOPE_AGGREGATES


@pytest.mark.parametrize("stored", ["garbage", "admin", "Roster", "", "0", None, 1])
def test_an_unrecognized_stored_scope_grants_nothing(stored: object) -> None:
    assert scope_from_stored(stored) is None


def test_a_roster_token_may_read_aggregates_but_not_the_reverse() -> None:
    assert scope_allows(SCOPE_ROSTER, SCOPE_AGGREGATES)
    assert scope_allows(SCOPE_ROSTER, SCOPE_ROSTER)
    assert scope_allows(SCOPE_AGGREGATES, SCOPE_AGGREGATES)
    assert not scope_allows(SCOPE_AGGREGATES, SCOPE_ROSTER)


@pytest.mark.asyncio
async def test_scope_comes_from_kv_then_d1() -> None:
    roster, legacy, from_d1 = "roster-secret", "legacy-secret", "d1-secret"
    kv = _Kv(
        {
            kv_key_for_hash(hash_token(roster)): "roster",
            kv_key_for_hash(hash_token(legacy)): "1",
        }
    )
    db = _Db({hash_token(from_d1): {"token_id": "t1", "scope": "roster"}})

    async def scope(secret: str) -> str | None:
        return await consumer_token_scope({"Authorization": f"Bearer {secret}"}, kv=kv, token_db=db)

    assert await scope(roster) == SCOPE_ROSTER
    assert await scope(legacy) == SCOPE_AGGREGATES
    assert await scope(from_d1) == SCOPE_ROSTER
    assert await scope("unknown") is None


@pytest.mark.asyncio
async def test_d1_row_without_a_scope_column_is_aggregates() -> None:
    db = _Db({hash_token("old"): {"token_id": "t0"}})
    result = await consumer_token_scope({"Authorization": "Bearer old"}, kv=_Kv({}), token_db=db)
    assert result == SCOPE_AGGREGATES


@pytest.mark.asyncio
async def test_an_unknown_scope_in_kv_or_d1_is_no_scope() -> None:
    kv = _Kv({kv_key_for_hash(hash_token("kv-odd")): "superuser"})
    db = _Db({hash_token("d1-odd"): {"token_id": "t2", "scope": "superuser"}})

    async def scope(secret: str) -> str | None:
        return await consumer_token_scope({"X-API-Key": secret}, kv=kv, token_db=db)

    assert await scope("kv-odd") is None
    assert await scope("d1-odd") is None


def _admin() -> ConsumerTokenAdmin:
    return ConsumerTokenAdmin(
        account_id=ACCOUNT, api_token="cf", d1_database_id=D1_ID, kv_namespace_id=KV_ID
    )


@respx.mock
def test_store_keeps_the_scope_in_d1_and_kv() -> None:
    inserts: list[list[str]] = []
    kv_writes: list[bytes] = []

    def d1(request: httpx.Request) -> httpx.Response:
        for item in json.loads(request.content)["batch"]:
            if item["sql"].startswith("SELECT name FROM pragma_table_info"):
                rows = [{"name": "token_id"}, {"name": "scope"}]
                return httpx.Response(
                    200, json={"success": True, "result": [{"results": rows, "success": True}]}
                )
            if item["sql"].startswith("INSERT INTO consumer_api_token"):
                inserts.append(item["params"])
        return httpx.Response(200, json={"success": True, "result": [{"success": True}]})

    respx.post(url__regex=rf".*/d1/database/{D1_ID}/query").mock(side_effect=d1)
    respx.put(url__regex=rf".*/namespaces/{KV_ID}/values/.*").mock(
        side_effect=lambda r: kv_writes.append(r.content) or httpx.Response(200, json={})
    )
    admin = _admin()
    issued = admin.issue("analyst", scope=SCOPE_ROSTER, days=30)
    admin.store(issued)
    admin.close()
    plaintext = issued.plaintext
    assert inserts[0][4] == "roster"
    assert inserts[0][1] == hash_token(plaintext)
    assert kv_writes == [b"roster"]


@respx.mock
def test_ensure_schema_adds_the_scope_and_expiry_columns_once() -> None:
    statements: list[str] = []
    columns = ["token_id"]

    def d1(request: httpx.Request) -> httpx.Response:
        for item in json.loads(request.content)["batch"]:
            statements.append(item["sql"])
            if item["sql"].startswith("SELECT name FROM pragma_table_info"):
                rows = [{"name": name} for name in columns]
                return httpx.Response(
                    200, json={"success": True, "result": [{"results": rows, "success": True}]}
                )
            if item["sql"].startswith("ALTER TABLE"):
                columns.append("scope" if " scope " in item["sql"] else "expires_at")
        return httpx.Response(200, json={"success": True, "result": [{"success": True}]})

    respx.post(url__regex=rf".*/d1/database/{D1_ID}/query").mock(side_effect=d1)
    admin = _admin()
    admin.ensure_schema()
    admin.ensure_schema()
    admin.close()
    alters = [s for s in statements if s.startswith("ALTER TABLE")]
    assert len(alters) == 2
    assert "DEFAULT 'aggregates'" in alters[0]
    assert alters[1].endswith("ADD COLUMN expires_at TEXT")

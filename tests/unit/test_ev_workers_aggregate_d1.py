"""WorkersAggregateStore against D1 stubs that mimic Python Workers bindings."""

from __future__ import annotations

import base64
import json

import pytest
from ev_api.bindings import WorkersAggregateStore

from texas_turnout_scraper.early_vote_sync import sha256_decoded


class _Prepared:
    def __init__(self, db: _PreviewD1, sql: str) -> None:
        self._db = db
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _Prepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        return self._db.first(self._sql, self._args)

    async def all(self) -> object:
        return self._db.all(self._sql, self._args)

    async def run(self) -> dict[str, object]:
        return self._db.run(self._sql, self._args)


class _PreviewD1:
    def __init__(
        self,
        *,
        pointer: str,
        file_id: str,
        sha256: str,
        decoded: bytes,
        days: list[dict[str, str]] | None = None,
    ) -> None:
        payload_b64 = base64.b64encode(decoded).decode()
        self._pointer = pointer
        self._file_id = file_id
        self._sha256 = sha256
        self._chunk_b64 = payload_b64
        self._days = days or []

    def prepare(self, sql: str) -> _Prepared:
        return _Prepared(self, sql)

    def first(self, sql: str, args: tuple[object, ...]) -> object | None:
        if "early_vote_pointer" in sql:
            return {"file_id": self._pointer}
        if "early_vote_day" in sql and "ev_date = ?" in sql:
            ev_date = str(args[1])
            for day in self._days:
                if day["ev_date"] == ev_date:
                    return day
            return None
        return None

    def run(self, sql: str, args: tuple[object, ...]) -> dict[str, object]:
        if "FROM early_vote_file" in sql:
            return {
                "results": [
                    {
                        "file_id": self._file_id,
                        "sha256": self._sha256,
                        "chunk_count": 1,
                        "chunks_present": 1,
                    }
                ],
                "success": True,
            }
        if "FROM early_vote_chunk" in sql:
            return {
                "results": [{"chunk_index": 0, "payload_b64": self._chunk_b64}],
                "success": True,
            }
        if "FROM early_vote_day" in sql and "ORDER BY" in sql:
            return {"results": list(self._days), "success": True}
        return {"results": [], "success": True}

    def all(self, sql: str, args: tuple[object, ...]) -> object:
        return self.run(sql, args)


def _turnout_bytes() -> bytes:
    payload = {
        "turnout_by_county": [
            {
                "name": "TRAVIS",
                "total_in_person_votes_for_election": 10,
                "total_mail_votes_for_election": 2,
            }
        ]
    }
    return json.dumps(payload).encode()


@pytest.fixture
def store_env() -> tuple[object, str]:
    decoded = _turnout_bytes()
    digest = sha256_decoded(decoded)
    db = _PreviewD1(
        pointer="f53815",
        file_id="f53815",
        sha256=digest,
        decoded=decoded,
        days=[
            {
                "ev_date": "2026-10-20",
                "file_id": "f53815",
                "sha256": digest,
            }
        ],
    )

    class _Env:
        DB = db

    return _Env(), digest


@pytest.mark.asyncio
async def test_workers_aggregate_store_totals_from_d1_run(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body = await store.build_body("53815", ("aggregates", "totals"), kv_sha256=digest)
    assert body is not None
    assert body["statewide"]["total"] == 12


@pytest.mark.asyncio
async def test_workers_aggregate_store_by_method_slice(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body = await store.build_body("53815", ("aggregates", "by-method"), kv_sha256=digest)
    assert body is not None
    assert body["statewide"]["in_person"] == 10


@pytest.mark.asyncio
async def test_workers_aggregate_store_by_day_series(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body = await store.build_body("53815", ("aggregates", "by-day"), kv_sha256=digest)
    assert body is not None
    assert body["series"][0]["ev_date"] == "2026-10-20"


@pytest.mark.asyncio
async def test_workers_aggregate_store_county_by_day(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body = await store.build_body("53815", ("aggregates", "county-by-day"), kv_sha256=digest)
    assert body is not None
    assert body["series"][0]["by_county"][0]["county"] == "TRAVIS"

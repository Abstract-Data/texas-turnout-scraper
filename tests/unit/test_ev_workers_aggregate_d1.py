"""WorkersAggregateStore against D1 stubs that mimic Python Workers bindings."""

from __future__ import annotations

import sqlite3

import pytest
from ev_api.bindings import WorkersAggregateStore

from tests.unit.test_early_vote_d1_aggregate import _AggregateD1, _turnout_bytes
from texas_turnout_scraper.early_vote_aggregates import aggregates_from_decoded
from texas_turnout_scraper.early_vote_d1_precompute_read import AggregatesNotComputed
from texas_turnout_scraper.early_vote_d1_precompute_store import (
    PRECOMPUTE_SCHEMA_CREATES,
    precompute_statements,
)
from texas_turnout_scraper.early_vote_sync import sha256_decoded


@pytest.fixture
def store_env() -> tuple[object, str]:
    decoded = _turnout_bytes()
    digest = sha256_decoded(decoded)
    db = _AggregateD1(
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
async def test_workers_aggregate_store_miss_raises_not_computed(
    store_env: tuple[object, str],
) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    with pytest.raises(AggregatesNotComputed):
        await store.build_body("53815", ("aggregates", "totals"), kv_sha256=digest)


@pytest.mark.asyncio
async def test_workers_aggregate_store_uses_precomputed_rows_when_present(
    store_env: tuple[object, str],
) -> None:
    env, digest = store_env
    decoded = _turnout_bytes()
    body = aggregates_from_decoded(decoded, source_election_id="53815", ev_date="2026-10-20")
    conn = sqlite3.connect(":memory:")
    for sql in PRECOMPUTE_SCHEMA_CREATES:
        conn.execute(sql)
    for sql, params in precompute_statements("53815", "f53815", body, ev_date_iso="2026-10-20"):
        conn.execute(sql, params)
    conn.commit()

    base_db = env.DB

    class _PrecomputedD1(_AggregateD1):
        def first(self, sql: str, args: tuple[object, ...]) -> object | None:
            if "early_vote_file_aggregate" in sql or "early_vote_county_aggregate" in sql:
                cur = conn.execute(sql, args)
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [c[0] for c in cur.description]
                return dict(zip(cols, row, strict=True))
            return super().first(sql, args)

        def all(self, sql: str, args: tuple[object, ...]) -> object:
            if "early_vote_county_aggregate" in sql and "ORDER BY" in sql:
                cur = conn.execute(sql, args)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
                return {"results": rows, "success": True}
            return super().all(sql, args)

    env.DB = _PrecomputedD1(
        pointer=base_db._pointer,
        file_id=base_db._file_id,
        sha256=base_db._sha256,
        decoded=base_db._decoded,
        days=base_db._days,
    )
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body_out = await store.build_body("53815", ("aggregates", "totals"), kv_sha256=digest)
    assert body_out is not None
    assert body_out["statewide"]["total"] == 18


@pytest.mark.asyncio
async def test_workers_aggregate_store_totals_from_precomputed(store_env: tuple[object, str]) -> None:
    await test_workers_aggregate_store_uses_precomputed_rows_when_present(store_env)


@pytest.mark.asyncio
async def test_workers_aggregate_store_by_method_slice(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    decoded = _turnout_bytes()
    body = aggregates_from_decoded(decoded, source_election_id="53815", ev_date="2026-10-20")
    conn = sqlite3.connect(":memory:")
    for sql in PRECOMPUTE_SCHEMA_CREATES:
        conn.execute(sql)
    for sql, params in precompute_statements("53815", "f53815", body, ev_date_iso="2026-10-20"):
        conn.execute(sql, params)
    conn.commit()
    base_db = env.DB

    class _PrecomputedD1(_AggregateD1):
        def first(self, sql: str, args: tuple[object, ...]) -> object | None:
            if "early_vote_file_aggregate" in sql:
                cur = conn.execute(sql, args)
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [c[0] for c in cur.description]
                return dict(zip(cols, row, strict=True))
            return super().first(sql, args)

        def all(self, sql: str, args: tuple[object, ...]) -> object:
            if "early_vote_county_aggregate" in sql:
                cur = conn.execute(sql, args)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
                return {"results": rows, "success": True}
            return super().all(sql, args)

    env.DB = _PrecomputedD1(
        pointer=base_db._pointer,
        file_id=base_db._file_id,
        sha256=base_db._sha256,
        decoded=base_db._decoded,
        days=base_db._days,
    )
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body_out = await store.build_body("53815", ("aggregates", "by-method"), kv_sha256=digest)
    assert body_out is not None
    assert body_out["statewide"]["in_person"] == 15

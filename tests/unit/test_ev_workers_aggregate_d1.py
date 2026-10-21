"""WorkersAggregateStore against D1 stubs that mimic Python Workers bindings."""

from __future__ import annotations

import pytest
from ev_api.bindings import WorkersAggregateStore

from tests.unit.test_early_vote_d1_aggregate import _AggregateD1, _turnout_bytes
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
async def test_workers_aggregate_store_totals_from_d1_run(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body = await store.build_body("53815", ("aggregates", "totals"), kv_sha256=digest)
    assert body is not None
    assert body["statewide"]["total"] == 18


@pytest.mark.asyncio
async def test_workers_aggregate_store_by_method_slice(store_env: tuple[object, str]) -> None:
    env, digest = store_env
    store = WorkersAggregateStore(env, {"53815": "DB"})
    body = await store.build_body("53815", ("aggregates", "by-method"), kv_sha256=digest)
    assert body is not None
    assert body["statewide"]["in_person"] == 15


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
    assert body["series"][0]["by_county"][0]["county"] == "HARRIS"

"""Key expiry: the pure arithmetic, the admin KV/D1 writes, and the Worker's rejection."""

from __future__ import annotations

import datetime as dt

import pytest
from ev_api.auth import consumer_token_scope
from hypothesis import given
from hypothesis import strategies as st

from texas_turnout_scraper.consumer_tokens import (
    KV_MIN_TTL_SECONDS,
    SCOPE_AGGREGATES,
    expiry_iso,
    extended_expiry,
    hash_token,
    is_expired,
    kv_key_for_hash,
    kv_ttl_seconds,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

instants = st.datetimes(
    min_value=dt.datetime(2000, 1, 1), max_value=dt.datetime(2100, 1, 1), timezones=st.just(UTC)
)
day_counts = st.integers(min_value=1, max_value=3650)


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


@given(now=instants, current=instants, days=day_counts)
def test_extend_never_shortens_and_never_lands_before_now_plus_n(
    now: dt.datetime, current: dt.datetime, days: int
) -> None:
    new = _parse(extended_expiry(current.isoformat(), now, days))
    window = dt.timedelta(days=days)
    assert new >= now.replace(microsecond=0) + window
    if current > now:
        assert new >= current.replace(microsecond=0) + window


@given(now=instants, days=day_counts)
def test_extend_a_token_without_expiry_is_now_plus_n(now: dt.datetime, days: int) -> None:
    expected = now.replace(microsecond=0) + dt.timedelta(days=days)
    assert _parse(extended_expiry(None, now, days)) == expected
    assert _parse(extended_expiry("", now, days)) == expected
    assert _parse(extended_expiry("garbage", now, days)) == expected


@given(now=instants, days=day_counts)
def test_a_new_expiry_is_not_expired_and_a_past_one_is(now: dt.datetime, days: int) -> None:
    assert not is_expired(expiry_iso(now, days), now)
    assert is_expired((now - dt.timedelta(seconds=1)).isoformat(), now)


def test_is_expired_boundary_is_inclusive() -> None:
    assert is_expired(NOW.isoformat(), NOW)
    assert not is_expired((NOW + dt.timedelta(seconds=1)).isoformat(), NOW)
    assert is_expired((NOW - dt.timedelta(seconds=1)).isoformat(), NOW)


@pytest.mark.parametrize("never", [None, ""])
def test_no_expiry_never_expires(never: object) -> None:
    assert not is_expired(never, NOW)


@pytest.mark.parametrize(
    "bad", ["not-a-date", "2026-13-45", "tomorrow", "   x", 12345, b"2099-01-01"]
)
def test_malformed_expiry_fails_closed(bad: object) -> None:
    assert is_expired(bad, NOW)


def test_naive_and_z_suffixed_instants_are_read_as_utc() -> None:
    assert not is_expired("2099-01-01T00:00:00", NOW)
    assert not is_expired("2099-01-01T00:00:00Z", NOW)
    assert is_expired("2000-01-01T00:00:00Z", NOW)


@given(now=instants, seconds=st.integers(min_value=-10_000, max_value=10**8))
def test_kv_ttl_respects_the_60_second_minimum(now: dt.datetime, seconds: int) -> None:
    ttl = kv_ttl_seconds((now + dt.timedelta(seconds=seconds)).isoformat(), now)
    assert ttl is not None
    assert ttl >= KV_MIN_TTL_SECONDS
    if seconds >= KV_MIN_TTL_SECONDS:
        assert ttl == seconds


def test_kv_ttl_is_none_without_an_expiry() -> None:
    assert kv_ttl_seconds(None, NOW) is None


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
    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self._rows = rows
        self._hash = ""

    def prepare(self, sql: str) -> _Db:
        return self

    def bind(self, digest: str) -> _Db:
        self._hash = digest
        return self

    async def first(self) -> object | None:
        return self._rows.get(self._hash)


def _iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(tz=UTC) + delta).isoformat()


async def _scope(secret: str, db: _Db, kv: _Kv | None = None) -> str | None:
    return await consumer_token_scope(
        {"Authorization": f"Bearer {secret}"}, kv=kv or _Kv({}), token_db=db
    )


@pytest.mark.asyncio
async def test_worker_rejects_an_expired_d1_row() -> None:
    db = _Db({hash_token("s"): {"token_id": "t", "expires_at": _iso(-dt.timedelta(seconds=5))}})
    assert await _scope("s", db) is None


@pytest.mark.asyncio
async def test_worker_accepts_a_future_expiry() -> None:
    db = _Db({hash_token("s"): {"token_id": "t", "expires_at": _iso(dt.timedelta(days=1))}})
    assert await _scope("s", db) == SCOPE_AGGREGATES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [{"token_id": "t"}, {"token_id": "t", "expires_at": None}, {"token_id": "t", "expires_at": ""}],
)
async def test_worker_accepts_a_row_without_expires_at(row: dict[str, object]) -> None:
    assert await _scope("s", _Db({hash_token("s"): row})) == SCOPE_AGGREGATES


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["soon", "2026-99-99", 42])
async def test_worker_rejects_a_malformed_expires_at(bad: object) -> None:
    db = _Db({hash_token("s"): {"token_id": "t", "expires_at": bad}})
    assert await _scope("s", db) is None


@pytest.mark.asyncio
async def test_kv_hit_is_trusted_because_the_entry_carries_a_ttl() -> None:
    kv = _Kv({kv_key_for_hash(hash_token("s")): "aggregates"})
    expired_row = _Db(
        {hash_token("s"): {"token_id": "t", "expires_at": _iso(-dt.timedelta(days=1))}}
    )
    assert await _scope("s", expired_row, kv) == SCOPE_AGGREGATES

"""Unit tests for D1 published-byte reads."""

from __future__ import annotations

import base64

import pytest

from texas_turnout_scraper import early_vote_d1_read
from texas_turnout_scraper.early_vote_d1_read import (
    D1OverloadedError,
    _d1_fetch_all,
    _d1_fetch_first,
    load_published_bytes,
    resolve_day_for_date,
)


class _FakePrepared:
    def __init__(self, db: _FakeDatabase, sql: str) -> None:
        self._db = db
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _FakePrepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        return self._db._first(self._sql, self._args)

    async def all(self) -> list[object]:
        return self._db._all(self._sql, self._args)


class _RunOnlyPrepared:
    """Python Workers D1 exposes ``run()``, not ``all()``."""

    def __init__(self, db: _RunOnlyDatabase, sql: str) -> None:
        self._db = db
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _RunOnlyPrepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        return None

    async def run(self) -> dict[str, object]:
        return self._db._run(self._sql, self._args)


class _RunOnlyDatabase:
    def __init__(
        self,
        *,
        files: list[dict[str, object]],
        chunks: dict[str, list[str]],
    ) -> None:
        self._files = files
        self._chunks = chunks

    def prepare(self, sql: str) -> _RunOnlyPrepared:
        return _RunOnlyPrepared(self, sql)

    def _run(self, sql: str, args: tuple[object, ...]) -> dict[str, object]:
        if "FROM early_vote_file" in sql:
            return {"results": list(self._files), "success": True}
        if "FROM early_vote_chunk" in sql:
            _election_id, file_id = args
            payloads = self._chunks.get(str(file_id), [])
            return {
                "results": [
                    {"chunk_index": index, "payload_b64": payload}
                    for index, payload in enumerate(payloads)
                ],
                "success": True,
            }
        return {"results": [], "success": True}


class _FakeDatabase:
    def __init__(
        self,
        *,
        files: list[dict[str, object]],
        chunks: dict[str, list[str]],
    ) -> None:
        self._files = files
        self._chunks = chunks

    def prepare(self, sql: str) -> _FakePrepared:
        return _FakePrepared(self, sql)

    def _first(self, sql: str, args: tuple[object, ...]) -> object | None:
        if "early_vote_day" in sql and "ev_date = ?" in sql:
            return None
        if "early_vote_pointer" in sql:
            return None
        return None

    def _all(self, sql: str, args: tuple[object, ...]) -> list[object]:
        if "FROM early_vote_file" in sql:
            return list(self._files)
        if "FROM early_vote_chunk" in sql:
            _election_id, file_id = args
            payloads = self._chunks.get(str(file_id), [])
            return [
                {"chunk_index": index, "payload_b64": payload}
                for index, payload in enumerate(payloads)
            ]
        return []


@pytest.mark.asyncio
async def test_load_published_bytes_uses_d1_run_when_all_missing() -> None:
    raw = b'{"turnout_by_county":[]}'
    payload = base64.b64encode(raw).decode()
    db = _RunOnlyDatabase(
        files=[
            {
                "file_id": "f1",
                "sha256": "day-hash",
                "chunk_count": 1,
                "chunks_present": 1,
            }
        ],
        chunks={"f1": [payload]},
    )
    result = await load_published_bytes(
        db,
        "53815",
        kv_sha256="kv-hash",
        file_id="f1",
        expected_sha256="day-hash",
    )
    assert result == raw


@pytest.mark.asyncio
async def test_load_published_bytes_rejects_incomplete_explicit_file_id() -> None:
    payload = base64.b64encode(b'{"turnout_by_county":[]}').decode()
    db = _FakeDatabase(
        files=[
            {
                "file_id": "f1",
                "sha256": "abc",
                "chunk_count": 2,
                "chunks_present": 1,
            }
        ],
        chunks={"f1": [payload]},
    )
    result = await load_published_bytes(
        db,
        "53815",
        kv_sha256="abc",
        file_id="f1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_load_published_bytes_rejects_explicit_file_id_wrong_hash() -> None:
    payload = base64.b64encode(b'{"turnout_by_county":[]}').decode()
    db = _FakeDatabase(
        files=[
            {
                "file_id": "f1",
                "sha256": "expected",
                "chunk_count": 1,
                "chunks_present": 1,
            }
        ],
        chunks={"f1": [payload]},
    )
    result = await load_published_bytes(
        db,
        "53815",
        kv_sha256="kv-hash",
        file_id="f1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_load_published_bytes_accepts_explicit_file_id_with_day_hash() -> None:
    raw = b'{"turnout_by_county":[]}'
    payload = base64.b64encode(raw).decode()
    db = _FakeDatabase(
        files=[
            {
                "file_id": "f1",
                "sha256": "day-hash",
                "chunk_count": 1,
                "chunks_present": 1,
            }
        ],
        chunks={"f1": [payload]},
    )
    result = await load_published_bytes(
        db,
        "53815",
        kv_sha256="kv-hash",
        file_id="f1",
        expected_sha256="day-hash",
    )
    assert result == raw


class _JsRow:
    """Minimal JsProxy stand-in: attribute access + ``to_py()``."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data[name]

    def to_py(self) -> dict[str, object]:
        return dict(self._data)


class _AllReturnsRunEnvelope:
    """Python Workers D1: ``all()`` may return the same shape as ``run()``."""

    def __init__(self, db: _RunOnlyDatabaseJsRows, sql: str) -> None:
        self._db = db
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _AllReturnsRunEnvelope:
        self._args = args
        return self

    async def first(self) -> object | None:
        return None

    async def all(self) -> dict[str, object]:
        return self._db._run(self._sql, self._args)


class _RunOnlyDatabaseJsRows(_RunOnlyDatabase):
    def _run(self, sql: str, args: tuple[object, ...]) -> dict[str, object]:
        if "FROM early_vote_file" in sql:
            return {
                "results": (
                    _JsRow(
                        {
                            "file_id": "f1",
                            "sha256": "day-hash",
                            "chunk_count": 1,
                            "chunks_present": 1,
                        }
                    ),
                ),
                "success": True,
            }
        if "FROM early_vote_chunk" in sql:
            _election_id, file_id = args
            payloads = self._chunks.get(str(file_id), [])
            return {
                "results": tuple(
                    _JsRow({"chunk_index": index, "payload_b64": payload})
                    for index, payload in enumerate(payloads)
                ),
                "success": True,
            }
        return {"results": (), "success": True}

    def prepare(self, sql: str) -> _AllReturnsRunEnvelope:
        return _AllReturnsRunEnvelope(self, sql)


@pytest.mark.asyncio
async def test_load_published_bytes_coerces_js_proxy_run_envelope() -> None:
    raw = (
        b'{"turnout_by_county":[{"name":"TRAVIS",'
        b'"total_in_person_votes_for_election":1,"total_mail_votes_for_election":0}]}'
    )
    payload = base64.b64encode(raw).decode()
    db = _RunOnlyDatabaseJsRows(
        files=[],
        chunks={"f1": [payload]},
    )
    result = await load_published_bytes(
        db,
        "53815",
        kv_sha256="kv-hash",
        file_id="f1",
        expected_sha256="day-hash",
    )
    assert result == raw


_OVERLOADED = (
    "Error: D1_ERROR: D1 DB is overloaded. Too many resources consumed within a 10 second window."
)


class _FlakyPrepared:
    """Raises each queued error in turn, then returns ``rows``."""

    def __init__(self, errors: list[Exception], rows: list[object]) -> None:
        self.errors = list(errors)
        self.rows = rows
        self.calls = 0

    def bind(self, *args: object) -> _FlakyPrepared:
        return self

    async def _answer(self) -> list[object]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.rows

    async def all(self) -> list[object]:
        return await self._answer()

    async def first(self) -> object | None:
        rows = await self._answer()
        return rows[0] if rows else None


class _FlakyDatabase:
    def __init__(self, prepared: _FlakyPrepared) -> None:
        self._prepared = prepared

    def prepare(self, sql: str) -> _FlakyPrepared:
        return self._prepared


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(early_vote_d1_read.asyncio, "sleep", record)
    return recorded


@pytest.mark.asyncio
async def test_fetch_all_returns_rows_once_d1_recovers(sleeps: list[float]) -> None:
    prepared = _FlakyPrepared([RuntimeError(_OVERLOADED)] * 2, [{"n": 1}])
    assert await _d1_fetch_all(prepared) == [{"n": 1}]
    assert prepared.calls == 3
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.5
    assert 2.0 <= sleeps[1] <= 3.0


@pytest.mark.asyncio
async def test_fetch_first_returns_row_once_d1_recovers(sleeps: list[float]) -> None:
    prepared = _FlakyPrepared([RuntimeError(_OVERLOADED)], [{"n": 1}])
    assert await _d1_fetch_first(prepared) == {"n": 1}
    assert prepared.calls == 2


@pytest.mark.asyncio
async def test_read_still_overloaded_after_retries_raises(sleeps: list[float]) -> None:
    prepared = _FlakyPrepared([RuntimeError(_OVERLOADED)] * 3, [])
    with pytest.raises(D1OverloadedError):
        await _d1_fetch_all(prepared)
    assert prepared.calls == 3


@pytest.mark.asyncio
async def test_other_d1_errors_are_not_retried(sleeps: list[float]) -> None:
    prepared = _FlakyPrepared([RuntimeError("D1_ERROR: no such table")], [{"n": 1}])
    with pytest.raises(RuntimeError, match="no such table"):
        await _d1_fetch_all(prepared)
    assert prepared.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_day_lookup_reports_overload_rather_than_no_such_day(sleeps: list[float]) -> None:
    db = _FlakyDatabase(_FlakyPrepared([RuntimeError(_OVERLOADED)] * 3, []))
    with pytest.raises(D1OverloadedError):
        await resolve_day_for_date(db, "53815", "2026-11-03")


@pytest.mark.asyncio
async def test_day_lookup_still_degrades_on_other_errors(sleeps: list[float]) -> None:
    db = _FlakyDatabase(_FlakyPrepared([RuntimeError("D1_ERROR: no such table")], []))
    assert await resolve_day_for_date(db, "53815", "2026-11-03") is None

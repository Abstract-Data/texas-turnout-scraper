"""Unit tests for D1 published-byte reads."""

from __future__ import annotations

import base64

import pytest

from texas_turnout_scraper.early_vote_d1_read import load_published_bytes


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

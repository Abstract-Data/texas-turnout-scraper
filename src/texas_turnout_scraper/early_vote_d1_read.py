"""Read published early-vote bytes from the per-election D1 chunk schema.

Matches :mod:`early_vote_sync` tables. Callers must pass the KV hash written
for the active version before loading chunk payloads.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_sync import EarlyVoteFileView, choose_published_file


class D1Prepared(Protocol):
    def bind(self, *args: object) -> D1Prepared: ...

    async def first(self) -> object | None: ...

    async def all(self) -> list[object]: ...


class D1Database(Protocol):
    def prepare(self, sql: str) -> D1Prepared: ...


_POINTER_SQL = "SELECT file_id FROM early_vote_pointer WHERE source_election_id = ?"
_FILE_SQL = (
    "SELECT f.file_id, f.sha256, f.chunk_count, "
    "(SELECT COUNT(*) FROM early_vote_chunk c "
    " WHERE c.source_election_id = f.source_election_id AND c.file_id = f.file_id) "
    "AS chunks_present "
    "FROM early_vote_file f WHERE f.source_election_id = ?"
)
_CHUNK_SQL = (
    "SELECT chunk_index, payload_b64 FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? ORDER BY chunk_index ASC"
)
_DAY_LIST_SQL = (
    "SELECT ev_date, file_id, sha256 FROM early_vote_day "
    "WHERE source_election_id = ? ORDER BY ev_date ASC"
)
_DAY_LOOKUP_SQL = (
    "SELECT ev_date, file_id, sha256 FROM early_vote_day "
    "WHERE source_election_id = ? AND ev_date = ?"
)


@dataclass(frozen=True)
class EvDayRow:
    ev_date: str
    file_id: str
    sha256: str


def _row_value(row: object, key: str) -> object | None:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _file_views(rows: list[object]) -> tuple[EarlyVoteFileView, ...]:
    views: list[EarlyVoteFileView] = []
    for row in rows:
        file_id = _row_value(row, "file_id")
        sha256 = _row_value(row, "sha256")
        chunk_count = _row_value(row, "chunk_count")
        chunks_present = _row_value(row, "chunks_present")
        if not isinstance(file_id, str) or not isinstance(sha256, str):
            continue
        if not isinstance(chunk_count, int):
            try:
                chunk_count = int(chunk_count)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        if not isinstance(chunks_present, int):
            try:
                chunks_present = int(chunks_present)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                chunks_present = 0
        views.append(
            EarlyVoteFileView(
                file_id=file_id,
                sha256=sha256,
                chunk_count=chunk_count,
                chunks_present=chunks_present,
            )
        )
    return tuple(views)


async def load_pointer_file_id(db: D1Database, source_election_id: str) -> str | None:
    election_id = require_election_id(source_election_id)
    row = await db.prepare(_POINTER_SQL).bind(election_id).first()
    if row is None:
        return None
    file_id = _row_value(row, "file_id")
    return file_id if isinstance(file_id, str) and file_id else None


async def load_file_views(db: D1Database, source_election_id: str) -> tuple[EarlyVoteFileView, ...]:
    election_id = require_election_id(source_election_id)
    rows = await db.prepare(_FILE_SQL).bind(election_id).all()
    return _file_views(rows)


async def load_day_rows(db: D1Database, source_election_id: str) -> tuple[EvDayRow, ...]:
    election_id = require_election_id(source_election_id)
    try:
        rows = await db.prepare(_DAY_LIST_SQL).bind(election_id).all()
    except Exception:
        return ()
    days: list[EvDayRow] = []
    for row in rows:
        ev_date = _row_value(row, "ev_date")
        file_id = _row_value(row, "file_id")
        sha256 = _row_value(row, "sha256")
        if not all(isinstance(item, str) and item for item in (ev_date, file_id, sha256)):
            continue
        days.append(EvDayRow(ev_date=ev_date, file_id=file_id, sha256=sha256))
    return tuple(days)


def _trusted_file_view(
    files: tuple[EarlyVoteFileView, ...],
    file_id: str,
    expected_sha256: str,
) -> EarlyVoteFileView | None:
    if expected_sha256 == "":
        return None
    for item in files:
        if item.file_id != file_id:
            continue
        if not item.complete or item.sha256 != expected_sha256:
            return None
        return item
    return None


async def resolve_day_for_date(
    db: D1Database,
    source_election_id: str,
    ev_date: str,
) -> EvDayRow | None:
    election_id = require_election_id(source_election_id)
    try:
        row = await db.prepare(_DAY_LOOKUP_SQL).bind(election_id, ev_date).first()
    except Exception:
        return None
    if row is None:
        return None
    resolved_date = _row_value(row, "ev_date")
    file_id = _row_value(row, "file_id")
    sha256 = _row_value(row, "sha256")
    if not all(isinstance(item, str) and item for item in (resolved_date, file_id, sha256)):
        return None
    return EvDayRow(ev_date=resolved_date, file_id=file_id, sha256=sha256)


async def resolve_file_id_for_day(
    db: D1Database,
    source_election_id: str,
    ev_date: str,
) -> str | None:
    day = await resolve_day_for_date(db, source_election_id, ev_date)
    return day.file_id if day is not None else None


async def load_chunks_b64(
    db: D1Database,
    source_election_id: str,
    file_id: str,
) -> list[str]:
    election_id = require_election_id(source_election_id)
    rows = await db.prepare(_CHUNK_SQL).bind(election_id, file_id).all()
    chunks: list[tuple[int, str]] = []
    for row in rows:
        index = _row_value(row, "chunk_index")
        payload = _row_value(row, "payload_b64")
        if not isinstance(payload, str):
            continue
        if not isinstance(index, int):
            try:
                index = int(index)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        chunks.append((index, payload))
    chunks.sort(key=lambda item: item[0])
    return [payload for _, payload in chunks]


def decode_chunk_payloads(chunks_b64: list[str]) -> bytes:
    parts = [base64.b64decode(piece) for piece in chunks_b64]
    return b"".join(parts)


async def load_published_bytes(
    db: D1Database,
    source_election_id: str,
    *,
    kv_sha256: str,
    file_id: str | None = None,
    expected_sha256: str | None = None,
) -> bytes | None:
    """Load one complete published file. ``file_id`` overrides pointer selection."""
    election_id = require_election_id(source_election_id)
    if file_id is None:
        pointer = await load_pointer_file_id(db, election_id)
        files = await load_file_views(db, election_id)
        published = choose_published_file(
            pointer_file_id=pointer,
            files=files,
            kv_sha256=kv_sha256,
        )
        if published is None:
            return None
        target_id = published.file_id
    else:
        want_hash = expected_sha256 if expected_sha256 is not None else kv_sha256
        files = await load_file_views(db, election_id)
        published = _trusted_file_view(files, file_id, want_hash)
        if published is None:
            return None
        target_id = published.file_id
    chunks = await load_chunks_b64(db, election_id, target_id)
    if not chunks:
        return None
    return decode_chunk_payloads(chunks)

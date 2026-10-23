"""Read stored roster files back out of the per-election D1 chunk tables, as a stream.

Used by the Worker's roster routes. Rows contain voter names and VUIDs, so nothing here
logs row content or puts it in an error. A day is served only when its file is complete and
its hash matches the day row, the same trust rule the aggregate reads use.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_read import (
    D1Database,
    _d1_fetch_all,
    _d1_int,
    _row_value,
)

DEFAULT_BATCH_CHUNKS = 64

_DAYS_SQL = (
    "SELECT d.ev_date AS ev_date, d.file_id AS file_id, d.sha256 AS sha256, "
    "f.byte_length AS byte_length, f.chunk_count AS chunk_count, "
    "(SELECT COUNT(*) FROM early_vote_chunk c "
    " WHERE c.source_election_id = f.source_election_id AND c.file_id = f.file_id) "
    "AS chunks_present, a.statewide_total AS rows "
    "FROM early_vote_day d "
    "JOIN early_vote_file f ON f.source_election_id = d.source_election_id "
    " AND f.file_id = d.file_id AND f.sha256 = d.sha256 "
    "LEFT JOIN early_vote_file_aggregate a ON a.source_election_id = d.source_election_id "
    " AND a.file_id = d.file_id "
    "WHERE d.source_election_id = ? ORDER BY d.ev_date ASC"
)
_CHUNKS_SQL = (
    "SELECT chunk_index, payload_b64 FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? AND chunk_index >= ? AND chunk_index < ? "
    "ORDER BY chunk_index ASC"
)
_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class RosterDay:
    ev_date: str
    file_id: str
    sha256: str
    byte_length: int
    chunk_count: int
    rows: int | None


async def list_days(db: D1Database, source_election_id: str) -> tuple[RosterDay, ...]:
    """Complete, hash-verified days for an election, oldest first."""
    election_id = require_election_id(source_election_id)
    rows = await _d1_fetch_all(db.prepare(_DAYS_SQL).bind(election_id))
    days: list[RosterDay] = []
    for row in rows:
        ev_date = _row_value(row, "ev_date")
        file_id = _row_value(row, "file_id")
        sha256 = _row_value(row, "sha256")
        byte_length = _d1_int(_row_value(row, "byte_length"))
        chunk_count = _d1_int(_row_value(row, "chunk_count"))
        present = _d1_int(_row_value(row, "chunks_present"))
        if not all(isinstance(v, str) and v for v in (ev_date, file_id, sha256)):
            continue
        if byte_length is None or chunk_count is None or chunk_count != present:
            continue
        days.append(
            RosterDay(
                ev_date=ev_date,
                file_id=file_id,
                sha256=sha256,
                byte_length=byte_length,
                chunk_count=chunk_count,
                rows=_d1_int(_row_value(row, "rows")),
            )
        )
    return tuple(days)


async def find_day(db: D1Database, source_election_id: str, ev_date: str) -> RosterDay | None:
    for day in await list_days(db, source_election_id):
        if day.ev_date == ev_date:
            return day
    return None


def day_etag(day: RosterDay) -> str:
    """Strong validator for one day's file: its content hash."""
    return day.sha256


def roster_etag(days: Sequence[RosterDay]) -> str:
    """Strong validator for the all-days CSV. Differs from any single day's, even for one day."""
    joined = "all\n" + "\n".join(f"{day.ev_date}:{day.sha256}" for day in days)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def _decoded_pieces(
    db: D1Database,
    election_id: str,
    day: RosterDay,
    batch_chunks: int,
) -> AsyncIterator[bytes]:
    for start in range(0, day.chunk_count, batch_chunks):
        stop = min(start + batch_chunks, day.chunk_count)
        rows = await _d1_fetch_all(
            db.prepare(_CHUNKS_SQL).bind(election_id, day.file_id, start, stop)
        )
        if len(rows) != stop - start:
            msg = "stored roster file is missing chunks"
            raise RuntimeError(msg)
        for row in rows:
            payload = _row_value(row, "payload_b64")
            if not isinstance(payload, str):
                msg = "stored roster chunk is not text"
                raise RuntimeError(msg)
            yield base64.b64decode(payload)


async def stream_day(
    db: D1Database,
    source_election_id: str,
    day: RosterDay,
    *,
    batch_chunks: int = DEFAULT_BATCH_CHUNKS,
) -> AsyncIterator[bytes]:
    """Yield the stored file's bytes unchanged."""
    election_id = require_election_id(source_election_id)
    async for piece in _decoded_pieces(db, election_id, day, batch_chunks):
        yield piece


async def stream_all_days(
    db: D1Database,
    source_election_id: str,
    days: Sequence[RosterDay],
    *,
    batch_chunks: int = DEFAULT_BATCH_CHUNKS,
) -> AsyncIterator[bytes]:
    """Yield every day as one CSV: a single header with ``EV_DATE`` first, each row dated.

    Rows are dated with one bulk ``bytes.replace`` per batch, not a Python call per line,
    because 2 million rows add up on the Worker's WASM runtime.
    """
    election_id = require_election_id(source_election_id)
    header_written = False
    for day in days:
        prefix = f"{day.ev_date},".encode()
        glue = b"\n" + prefix
        pending = b""
        header_pending = True
        async for piece in _decoded_pieces(db, election_id, day, batch_chunks):
            pending += piece
            if header_pending:
                newline = pending.find(b"\n")
                if newline < 0:
                    continue
                header_pending = False
                header, pending = pending[:newline], pending[newline + 1 :]
                if not header_written:
                    header_written = True
                    yield b"EV_DATE," + header.removeprefix(_BOM) + b"\n"
            cut = pending.rfind(b"\n")
            if cut < 0:
                continue
            body, pending = pending[: cut + 1], pending[cut + 1 :]
            yield (prefix + body.replace(b"\n", glue))[: -len(prefix)]
        if header_pending and pending.strip(b"\r"):
            if not header_written:
                header_written = True
                yield b"EV_DATE," + pending.removeprefix(_BOM) + b"\n"
        elif pending.strip(b"\r"):
            yield prefix + pending + b"\n"

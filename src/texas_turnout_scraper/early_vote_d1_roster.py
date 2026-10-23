"""Read stored roster files back out of the per-election D1 chunk tables, as a stream.

Used by the Worker's roster routes. Rows contain voter names and VUIDs, so nothing here
logs row content or puts it in an error. A day is served only when its file is complete and
its hash matches the day row, the same trust rule the aggregate reads use.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

from texas_turnout_scraper import early_vote_sync
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


def _chunk_size() -> int:
    return early_vote_sync.CHUNK_RAW_BYTES


def range_supported(day: RosterDay) -> bool:
    """True when the stored chunk count matches fixed-size chunks, so offsets map to chunks."""
    size = _chunk_size()
    return day.byte_length > 0 and day.chunk_count == -(-day.byte_length // size)


async def _fetch_batch(
    db: D1Database, election_id: str, day: RosterDay, start: int, stop: int
) -> list[bytes]:
    rows = await _d1_fetch_all(db.prepare(_CHUNKS_SQL).bind(election_id, day.file_id, start, stop))
    if len(rows) != stop - start:
        msg = "stored roster file is missing chunks"
        raise RuntimeError(msg)
    decoded: list[bytes] = []
    for row in rows:
        payload = _row_value(row, "payload_b64")
        if not isinstance(payload, str):
            msg = "stored roster chunk is not text"
            raise RuntimeError(msg)
        decoded.append(base64.b64decode(payload))
    return decoded


async def _decoded_pieces(
    db: D1Database,
    election_id: str,
    day: RosterDay,
    batch_chunks: int,
    first_chunk: int = 0,
    stop_chunk: int | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield decoded chunks, asking D1 for the next batch while the current one is sent."""
    end = day.chunk_count if stop_chunk is None else min(stop_chunk, day.chunk_count)
    spans = [
        (start, min(start + batch_chunks, end)) for start in range(first_chunk, end, batch_chunks)
    ]
    pending: asyncio.Future[list[bytes]] | None = None
    try:
        if spans:
            pending = asyncio.ensure_future(_fetch_batch(db, election_id, day, *spans[0]))
        for index, current in enumerate(spans):
            if pending is None:
                msg = f"no fetch scheduled for span {current}"
                raise RuntimeError(msg)
            batch = await pending
            pending = (
                asyncio.ensure_future(_fetch_batch(db, election_id, day, *spans[index + 1]))
                if index + 1 < len(spans)
                else None
            )
            for piece in batch:
                yield piece
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def _slice(
    pieces: AsyncGenerator[bytes, None], skip: int, take: int
) -> AsyncGenerator[bytes, None]:
    async with contextlib.aclosing(pieces) as source:
        async for piece in source:
            if take <= 0:
                break
            if skip >= len(piece):
                skip -= len(piece)
                continue
            part = piece[skip : skip + take]
            skip = 0
            take -= len(part)
            yield part


async def stream_day(
    db: D1Database,
    source_election_id: str,
    day: RosterDay,
    *,
    batch_chunks: int = DEFAULT_BATCH_CHUNKS,
    start: int = 0,
    stop: int | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield the stored file's bytes unchanged, or bytes ``[start, stop)`` of it."""
    election_id = require_election_id(source_election_id)
    if start == 0 and (stop is None or stop >= day.byte_length):
        whole = _decoded_pieces(db, election_id, day, batch_chunks)
        async with contextlib.aclosing(whole) as source:
            async for piece in source:
                yield piece
        return
    if not range_supported(day):
        msg = "byte ranges need fixed-size chunks"
        raise ValueError(msg)
    end = day.byte_length if stop is None else min(stop, day.byte_length)
    if start < 0 or start >= end:
        return
    size = _chunk_size()
    pieces = _decoded_pieces(db, election_id, day, batch_chunks, start // size, -(-end // size))
    sliced = _slice(pieces, start % size, end - start)
    async with contextlib.aclosing(sliced) as source:
        async for part in source:
            yield part


async def stream_all_days(
    db: D1Database,
    source_election_id: str,
    days: Sequence[RosterDay],
    *,
    batch_chunks: int = DEFAULT_BATCH_CHUNKS,
) -> AsyncGenerator[bytes, None]:
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
        stored = _decoded_pieces(db, election_id, day, batch_chunks)
        async with contextlib.aclosing(stored) as source:
            async for piece in source:
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

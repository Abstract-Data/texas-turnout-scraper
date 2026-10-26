"""Temporary spike routes: stream-canonicalize a roster day, scatter it to R2, write one range.

``GET /spike/stage`` makes one portal request, streams the envelope through the first spike's
``EnvelopeStreamDecoder``, canonicalizes the decoded bytes incrementally, hashes the raw and the
canonical bytes, writes the canonical object by 8 MiB multipart upload and the per-range
``(row_hash, cells)`` segments, and reports counts, hashes and timings. ``cleanup=1`` deletes the
run's staging objects instead.

``POST /spike/range-write`` reads one hash range's segments back, numbers duplicates (``occ``),
validates the rows with the loader's SQLModel models and writes the range to the scratch D1.
``verify=1`` computes and reports without writing.

Neither route returns row content. Delete this file, its tests,
``scripts/spike_compare_canonical.py``, the ``sink`` hook in ``spike_portal`` and the mount line
in ``app.py`` to remove the spike.
"""

from __future__ import annotations

import bisect
import codecs
import contextlib
import csv
import hashlib
import io
import json
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from ev_api.auth import read_request_authorized
from ev_api.spike_portal import (
    _DATE_RE,
    ELECTION_CALENDARS,
    SPIKE_USER_AGENT,
    EnvelopeError,
    EnvelopeStreamDecoder,
    Fetch,
    _CallGate,
    _elapsed_ms,
    _selected_headers,
    _utc_today,
    _workers_fetch,
    body_chunks,
    looks_like_html_challenge,
    memory_note,
    portal_url,
    stored_day_hash,
)
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_roster_rows import (
    KEY_RANGES,
    ROW_SCHEMA_CREATES,
    ROWS_PER_STATEMENT,
    RosterSchemaError,
    _feed,
    day_number,
    header_fields,
    insert_statements,
    key_ranges,
    reject_statements,
)

SEGMENT_FLUSH_BYTES = 512 * 1024
MULTIPART_PART_BYTES = 8 * 1024 * 1024
STATEMENTS_PER_BATCH = 4
MAX_LISTED_OBJECTS = 20_000
_SNIFF_BYTES = 512
_MAX_SUFFIX = 32
_SUFFIX_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_CANONICAL_NAME = "file.csv"
_HEADER_NAME = "header.json"
_VERIFY_SAMPLE = 5
_SCRATCH_VERSION = 1

_CPU_NOTE = (
    "Workers clocks advance on I/O only, so every *_ms here counts network and binding waits "
    "and not the CPU spent decoding, canonicalizing or hashing; read CPU ms for this request "
    "from the Workers logs (wrangler tail, or the Workers Observability invocation record)"
)
_OCC_NOTE = (
    "occ is not computed in stage: identical lines share a row_hash and therefore a range, so "
    "range-write numbers the duplicates it reads and gets the same occ as keyed_lines"
)


class SpikeRequestError(ValueError):
    """The query string is not one these routes act on. Nothing was fetched or written."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------------------
# Streaming canonicalizer
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalRecord:
    """One record the canonicalizer emitted, with the exact bytes it contributed."""

    cells: tuple[str, ...]
    canonical: bytes
    is_header: bool


class _NeedMoreInput(Exception):
    """Raised inside the csv reader's source while the current record is still open."""


class _LineSource:
    """Hands buffered lines to one ``csv.reader`` and counts how many it took."""

    def __init__(self, lines: list[str], *, final: bool) -> None:
        self._lines = lines
        self._final = final
        self.taken = 0

    def __iter__(self) -> _LineSource:
        return self

    def __next__(self) -> str:
        if self.taken < len(self._lines):
            line = self._lines[self.taken]
            self.taken += 1
            return line
        if self._final:
            raise StopIteration
        raise _NeedMoreInput


class StreamingCanonicalizer:
    r"""``canonicalize_roster_csv`` over a stream: same records, same bytes, bounded memory.

    The reference runs ``csv.reader`` over ``decoded.decode("utf-8-sig")`` in a ``StringIO``,
    whose line iteration splits on ``\n`` only and keeps the terminator. This feeds a
    ``csv.reader`` exactly those lines, so quoted newlines survive and cells holding ``\r``,
    ``\x0b``, ``\x0c``, ``\x85`` or ``U+2028`` are not split. A record still open when the
    buffer runs out is re-read once more input arrives, which is why memory is one chunk plus
    one partial record rather than the whole file.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")()
        self._text = ""
        self._pending: list[str] = []
        self._sink = io.StringIO()
        self._writer = csv.writer(self._sink, lineterminator="\n")
        self._header: tuple[str, ...] | None = None

    @property
    def header(self) -> tuple[str, ...] | None:
        return self._header

    def feed(self, chunk: bytes) -> list[CanonicalRecord]:
        self._text += self._decoder.decode(chunk)
        self._buffer_lines()
        return self._drain(final=False)

    def finish(self) -> list[CanonicalRecord]:
        self._text += self._decoder.decode(b"", True)
        self._buffer_lines()
        if self._text:
            self._pending.append(self._text)
            self._text = ""
        return self._drain(final=True)

    def _buffer_lines(self) -> None:
        if "\n" not in self._text:
            return
        head, _, self._text = self._text.rpartition("\n")
        self._pending.extend(line + "\n" for line in (head + "\n").split("\n")[:-1])

    def _drain(self, *, final: bool) -> list[CanonicalRecord]:
        out: list[CanonicalRecord] = []
        while self._pending or final:
            source = _LineSource(self._pending, final=final)
            try:
                row = next(csv.reader(source))
            except _NeedMoreInput:
                return out
            except StopIteration:
                self._pending = []
                return out
            self._pending = self._pending[source.taken :]
            record = self._record(row)
            if record is not None:
                out.append(record)
            if not self._pending and not final:
                return out
        return out

    def _record(self, row: list[str]) -> CanonicalRecord | None:
        if self._header is None:
            self._header = tuple(row)
            return CanonicalRecord(tuple(row), self._emit(row), is_header=True)
        if not row:
            return None
        return CanonicalRecord(tuple(row), self._emit(row), is_header=False)

    def _emit(self, row: list[str]) -> bytes:
        self._writer.writerow(row)
        text = self._sink.getvalue()
        self._sink.seek(0)
        self._sink.truncate(0)
        return text.encode("utf-8")


# --------------------------------------------------------------------------------------
# Row keys and the hash-range scatter
# --------------------------------------------------------------------------------------


def header_seed(header: Sequence[str]) -> Any:
    """The blake2b seed ``keyed_lines`` starts every row from: the header's length and cells."""
    seed = hashlib.blake2b(digest_size=8)
    seed.update(len(header).to_bytes(4, "big"))
    _feed(seed, header)
    return seed


def row_hash(seed: Any, cells: Sequence[str]) -> int:
    hasher = seed.copy()
    _feed(hasher, cells)
    return int.from_bytes(hasher.digest(), "big", signed=True)


_RANGE_LOWS = tuple(int(low) for low, _ in key_ranges())


def range_of(value: int) -> int:
    """Which ``key_ranges()`` bucket a signed 64-bit ``row_hash`` falls in."""
    return bisect.bisect_right(_RANGE_LOWS, value) - 1


@dataclass(frozen=True)
class Segment:
    """One immutable staging object: a range's rows in file order, as JSON lines."""

    range_index: int
    seq: int
    data: bytes


def segment_line(digest: int, cells: Sequence[str]) -> bytes:
    return (
        json.dumps([digest, list(cells)], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def read_segment(data: bytes) -> Iterator[tuple[int, list[str]]]:
    for line in data.splitlines():
        if not line:
            continue
        digest, cells = json.loads(line)
        yield int(digest), [str(cell) for cell in cells]


class RowScatter:
    """Buffers ``(row_hash, cells)`` per hash range, handing out a segment at every flush size."""

    def __init__(self, header: Sequence[str], *, flush_bytes: int = SEGMENT_FLUSH_BYTES) -> None:
        self._seed = header_seed(header)
        self._flush_bytes = flush_bytes
        self._buffers: list[list[bytes]] = [[] for _ in range(KEY_RANGES)]
        self._sizes = [0] * KEY_RANGES
        self.rows_per_range = [0] * KEY_RANGES
        self.segments_per_range = [0] * KEY_RANGES

    def add(self, cells: Sequence[str]) -> tuple[int, Segment | None]:
        digest = row_hash(self._seed, cells)
        index = range_of(digest)
        encoded = segment_line(digest, cells)
        self._buffers[index].append(encoded)
        self._sizes[index] += len(encoded)
        self.rows_per_range[index] += 1
        if self._sizes[index] >= self._flush_bytes:
            return digest, self._take(index)
        return digest, None

    def flush_all(self) -> Iterator[Segment]:
        for index in range(KEY_RANGES):
            if self._buffers[index]:
                yield self._take(index)

    def _take(self, index: int) -> Segment:
        data = b"".join(self._buffers[index])
        self._buffers[index] = []
        self._sizes[index] = 0
        self.segments_per_range[index] += 1
        return Segment(index, self.segments_per_range[index] - 1, data)


@dataclass
class CountyMethodCounts:
    """Rows per (county, voting method). Those two column values only, never names or VUIDs."""

    county_at: int | None = None
    method_at: int | None = None
    counts: dict[tuple[str, str], int] = field(default_factory=dict)

    @classmethod
    def for_header(cls, header: Sequence[str]) -> CountyMethodCounts:
        at = {name.strip().upper(): position for position, name in enumerate(header)}
        return cls(county_at=at.get("COUNTY"), method_at=at.get("VOTING_METHOD"))

    def add(self, cells: Sequence[str]) -> None:
        if self.county_at is None or self.method_at is None:
            return
        if self.county_at >= len(cells) or self.method_at >= len(cells):
            return
        key = (cells[self.county_at].strip(), cells[self.method_at].strip().upper())
        self.counts[key] = self.counts.get(key, 0) + 1

    def as_report(self) -> list[dict[str, object]]:
        return [
            {"county": county, "voting_method": method, "rows": rows}
            for (county, method), rows in sorted(self.counts.items())
        ]


# --------------------------------------------------------------------------------------
# Staging bucket boundary
# --------------------------------------------------------------------------------------


class MultipartTarget(Protocol):
    async def upload_part(self, part_number: int, data: bytes) -> None: ...

    async def complete(self) -> None: ...

    async def abort(self) -> None: ...


class StagingBucket(Protocol):
    """The slice of the R2 binding this spike needs, so the logic runs against a fake."""

    async def put(self, key: str, data: bytes) -> None: ...

    async def multipart(self, key: str) -> MultipartTarget: ...

    async def keys(self, prefix: str) -> list[str]: ...

    async def read(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> None: ...


def _js_bytes(data: bytes) -> Any:
    """A JS ``Uint8Array`` holding a copy of ``data``.

    ``uploadPart`` and ``put`` take ``ReadableStream | ArrayBuffer | ArrayBufferView | string |
    Blob`` (R2 "Workers API reference"), and Pyodide does not convert ``bytes`` implicitly, so
    the copy is explicit. ``JsBuffer.assign`` is the cheap path; ``to_js`` is the fallback the
    Python Workers FFI page documents under "Converting Python to JavaScript".
    """
    from js import Uint8Array

    view = Uint8Array.new(len(data))
    assign = getattr(view, "assign", None)
    if assign is not None:
        assign(data)
        return view
    from pyodide.ffi import to_js

    return to_js(data)


class _R2Multipart:
    def __init__(self, upload: Any) -> None:
        self._upload = upload
        self._parts: list[Any] = []

    async def upload_part(self, part_number: int, data: bytes) -> None:
        self._parts.append(await self._upload.uploadPart(part_number, _js_bytes(data)))

    async def complete(self) -> None:
        from pyodide.ffi import to_js

        await self._upload.complete(to_js(self._parts))

    async def abort(self) -> None:
        await self._upload.abort()


class R2StagingBucket:
    """``StagingBucket`` over a Workers R2 binding (R2 "Workers API reference", Python tab)."""

    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    async def put(self, key: str, data: bytes) -> None:
        await self._bucket.put(key, _js_bytes(data))

    async def multipart(self, key: str) -> MultipartTarget:
        return _R2Multipart(await self._bucket.createMultipartUpload(key))

    async def keys(self, prefix: str) -> list[str]:
        from js import Object
        from pyodide.ffi import to_js

        found: list[str] = []
        cursor: str | None = None
        while True:
            options: dict[str, object] = {"prefix": prefix, "limit": 1000}
            if cursor is not None:
                options["cursor"] = cursor
            listing = await self._bucket.list(to_js(options, dict_converter=Object.fromEntries))
            found.extend(str(entry.key) for entry in listing.objects)
            if not bool(listing.truncated) or len(found) >= MAX_LISTED_OBJECTS:
                return sorted(found)
            cursor = str(listing.cursor)

    async def read(self, key: str) -> bytes | None:
        from js import Uint8Array

        stored = await self._bucket.get(key)
        if stored is None:
            return None
        return bytes(Uint8Array.new(await stored.arrayBuffer()).to_bytes())

    async def delete(self, key: str) -> None:
        await self._bucket.delete(key)


# --------------------------------------------------------------------------------------
# Validated requests
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StageRequest:
    """A validated call. Built at the boundary; everything downstream trusts it."""

    election_id: str
    ev_date: str
    day: date
    suffix: str

    @property
    def prefix(self) -> str:
        return f"staging/spike-{self.suffix}/{self.ev_date}"

    @property
    def canonical_key(self) -> str:
        return f"{self.prefix}/{_CANONICAL_NAME}"

    @property
    def header_key(self) -> str:
        return f"{self.prefix}/{_HEADER_NAME}"

    def segment_key(self, range_index: int, seq: int) -> str:
        return f"{self.prefix}/{range_index:02d}/{seq:06d}"

    def range_prefix(self, range_index: int) -> str:
        return f"{self.prefix}/{range_index:02d}/"


def _checked_suffix(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text or len(text) > _MAX_SUFFIX or not set(text) <= _SUFFIX_ALPHABET:
        raise SpikeRequestError("bad_suffix")
    return text


def parse_stage_request(
    election_id: str | None,
    ev_date: str | None,
    suffix: str | None,
    *,
    today: date,
) -> StageRequest:
    """The same guards as ``/spike/portal-fetch``, plus the staging-prefix suffix."""
    try:
        normalized = require_election_id(election_id)
    except ValueError as exc:
        raise SpikeRequestError("bad_request") from exc
    if ev_date is None or _DATE_RE.fullmatch(ev_date) is None:
        raise SpikeRequestError("bad_request")
    try:
        day = date.fromisoformat(ev_date)
    except ValueError as exc:
        raise SpikeRequestError("bad_request") from exc
    window = ELECTION_CALENDARS.get(normalized)
    if window is None:
        raise SpikeRequestError("election_not_allowed")
    if day > today:
        raise SpikeRequestError("date_in_future")
    if not window[0] <= day <= window[1]:
        raise SpikeRequestError("date_outside_calendar")
    return StageRequest(normalized, ev_date, day, _checked_suffix(suffix))


def checked_range(raw: str | None) -> int:
    if raw is None or not raw.strip().isdigit():
        raise SpikeRequestError("bad_range")
    index = int(raw)
    if not 0 <= index < KEY_RANGES:
        raise SpikeRequestError("bad_range")
    return index


# --------------------------------------------------------------------------------------
# Stage: one pass over the day file
# --------------------------------------------------------------------------------------


class _PartWriter:
    """Fixed-size multipart parts over a stream of pieces. The upload starts on the first part."""

    def __init__(self, bucket: StagingBucket, key: str, *, part_bytes: int) -> None:
        self._bucket = bucket
        self._key = key
        self.part_bytes = part_bytes
        self._buffer = bytearray()
        self._target: MultipartTarget | None = None
        self.parts = 0
        self.bytes_written = 0

    async def add(self, data: bytes) -> None:
        if not data:
            return
        self._buffer += data
        while len(self._buffer) >= self.part_bytes:
            await self._send(bytes(self._buffer[: self.part_bytes]))
            del self._buffer[: self.part_bytes]

    async def finish(self) -> None:
        if self._target is None and not self._buffer:
            return
        if self._buffer:
            await self._send(bytes(self._buffer))
            self._buffer.clear()
        target = self._target
        if target is not None:
            await target.complete()

    async def abort(self) -> None:
        target = self._target
        if target is not None:
            with contextlib.suppress(Exception):
                await target.abort()

    async def _send(self, data: bytes) -> None:
        if self._target is None:
            self._target = await self._bucket.multipart(self._key)
        self.parts += 1
        self.bytes_written += len(data)
        await self._target.upload_part(self.parts, data)


class StagePipeline:
    """Decoded bytes in, canonical object and range segments out. Writes start on the first part."""

    def __init__(
        self,
        bucket: StagingBucket,
        request: StageRequest,
        *,
        part_bytes: int = MULTIPART_PART_BYTES,
        flush_bytes: int = SEGMENT_FLUSH_BYTES,
    ) -> None:
        self._bucket = bucket
        self._request = request
        self._flush_bytes = flush_bytes
        self._canonicalizer = StreamingCanonicalizer()
        self._canonical_sha = hashlib.sha256()
        self._parts = _PartWriter(bucket, request.canonical_key, part_bytes=part_bytes)
        self._scatter: RowScatter | None = None
        self._counts = CountyMethodCounts()
        self._pending_canonical = bytearray()
        self._queued: list[Segment] = []
        self._header: tuple[str, ...] | None = None
        self._schema_error: str | None = None
        self.rows = 0
        self.canonical_bytes = 0
        self.segments_written = 0
        self.segment_bytes = 0

    def take(self, decoded: bytes) -> None:
        """The envelope decoder's sink: called for every decoded piece inside the stream loop."""
        self._consume(self._canonicalizer.feed(decoded))

    def _consume(self, records: list[CanonicalRecord]) -> None:
        for record in records:
            self.canonical_bytes += len(record.canonical)
            self._canonical_sha.update(record.canonical)
            self._pending_canonical += record.canonical
            if record.is_header:
                self._start_header(record)
                continue
            self.rows += 1
            self._counts.add(record.cells)
            scatter = self._scatter
            if scatter is None:
                continue
            _, segment = scatter.add(record.cells)
            if segment is not None:
                self._queued.append(segment)

    def _start_header(self, record: CanonicalRecord) -> None:
        self._header = record.cells
        self._scatter = RowScatter(record.cells, flush_bytes=self._flush_bytes)
        self._counts = CountyMethodCounts.for_header(record.cells)
        try:
            header_fields(record.canonical)
        except (RosterSchemaError, csv.Error) as exc:
            self._schema_error = type(exc).__name__

    async def drain(self) -> None:
        """Write whatever the stream has produced so far: canonical parts, then full segments."""
        pending = bytes(self._pending_canonical)
        self._pending_canonical.clear()
        await self._parts.add(pending)
        while self._queued:
            await self._write_segment(self._queued.pop(0))

    async def _write_segment(self, segment: Segment) -> None:
        await self._bucket.put(
            self._request.segment_key(segment.range_index, segment.seq), segment.data
        )
        self.segments_written += 1
        self.segment_bytes += len(segment.data)

    async def finish(self) -> None:
        self._consume(self._canonicalizer.finish())
        scatter = self._scatter
        if scatter is not None:
            self._queued.extend(scatter.flush_all())
        await self.drain()
        if self._header is not None:
            await self._bucket.put(
                self._request.header_key,
                json.dumps(list(self._header), ensure_ascii=False).encode("utf-8"),
            )
        await self._parts.finish()

    async def abort(self) -> None:
        await self._parts.abort()

    def report(self, *, complete: bool) -> dict[str, object]:
        scatter = self._scatter
        return {
            "rows": self.rows,
            "header_columns": (
                list(self._canonicalizer.header or ()) if self._schema_error is None else None
            ),
            "header_column_count": len(self._canonicalizer.header or ()),
            "header_schema_error": self._schema_error,
            "canonical_bytes": self.canonical_bytes,
            "sha256": self._canonical_sha.hexdigest() if complete else None,
            "canonical_key": self._request.canonical_key if complete else None,
            "multipart_parts": self._parts.parts,
            "multipart_bytes": self._parts.bytes_written,
            "multipart_part_bytes": self._parts.part_bytes,
            "upload_mode": "multipart",
            "rows_per_range": list(scatter.rows_per_range) if scatter else [0] * KEY_RANGES,
            "segments_per_range": list(scatter.segments_per_range) if scatter else [0] * KEY_RANGES,
            "segments_written": self.segments_written,
            "segment_bytes": self.segment_bytes,
            "county_method_counts": self._counts.as_report(),
            "wrote_anything": self._parts.parts > 0 or self.segments_written > 0,
        }


async def stage_day(
    fetch: Fetch,
    url: str,
    bucket: StagingBucket,
    request: StageRequest,
    *,
    clock: Callable[[], float] = time.perf_counter,
    part_bytes: int = MULTIPART_PART_BYTES,
    flush_bytes: int = SEGMENT_FLUSH_BYTES,
) -> dict[str, object]:
    """One portal request, canonicalized, hashed and scattered in a single pass. Counts only."""
    started = clock()
    report: dict[str, object] = {
        "http_status": None,
        "response_headers": None,
        "ttfb_ms": None,
        "body_looks_like_html_challenge": False,
        "error": None,
    }
    decoder = EnvelopeStreamDecoder()
    pipeline = StagePipeline(bucket, request, part_bytes=part_bytes, flush_bytes=flush_bytes)
    decoder.sink = pipeline.take
    complete = False
    try:
        complete = await _stream_day(fetch, url, decoder, pipeline, report, clock, started)
    except EnvelopeError as exc:
        report["error"] = {"class": type(exc).__name__, "reason": exc.reason}
    except csv.Error:
        # csv.Error's own name is just "Error", and the day file is what the reference would
        # reject too, so name the stage that failed instead of the class.
        report["error"] = {"class": "csv.Error", "reason": "canonicalize_rejected"}
    except Exception as exc:  # noqa: BLE001 - report the class only, never the message
        report["error"] = {"class": type(exc).__name__}
    if not complete:
        await pipeline.abort()
    report.update(pipeline.report(complete=complete))
    report.update(
        {
            "total_ms": _elapsed_ms(clock, started),
            "decoded_bytes": decoder.decoded_bytes,
            "raw_sha256": decoder.decoded_sha256 if complete else None,
            "chunks": decoder.chunks,
            "peak_memory_note": memory_note(),
            "cpu_note": _CPU_NOTE,
            "occ_note": _OCC_NOTE,
        }
    )
    return report


async def _stream_day(
    fetch: Fetch,
    url: str,
    decoder: EnvelopeStreamDecoder,
    pipeline: StagePipeline,
    report: dict[str, object],
    clock: Callable[[], float],
    started: float,
) -> bool:
    response = await fetch(url, {"User-Agent": SPIKE_USER_AGENT, "Accept": "application/json"})
    status = int(response.status)
    selected = _selected_headers(response.headers)
    report.update(
        {
            "http_status": status,
            "response_headers": selected,
            "ttfb_ms": _elapsed_ms(clock, started),
        }
    )
    challenge = looks_like_html_challenge(selected["content-type"], b"")
    head = bytearray()
    if response.body is not None:
        async with contextlib.aclosing(body_chunks(response.body)) as pieces:
            async for piece in pieces:
                if len(head) < _SNIFF_BYTES:
                    head += piece[: _SNIFF_BYTES - len(head)]
                    challenge = looks_like_html_challenge(selected["content-type"], bytes(head))
                if status != 200 or challenge:
                    break
                decoder.feed(piece)
                await pipeline.drain()
                if decoder.done:
                    break
    report["body_looks_like_html_challenge"] = challenge
    if status != 200 or challenge:
        return False
    decoder.finish()
    await pipeline.finish()
    return True


async def cleanup_run(bucket: StagingBucket, request: StageRequest) -> dict[str, object]:
    """Delete every object this run wrote under its own prefix."""
    keys = await bucket.keys(f"{request.prefix}/")
    for key in keys:
        await bucket.delete(key)
    return {
        "mode": "cleanup",
        "prefix": f"{request.prefix}/",
        "objects_deleted": len(keys),
        "cache_control": "no-store",
    }


# --------------------------------------------------------------------------------------
# Range write to the scratch D1
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeRows:
    """One range's rows in file order, with the duplicate numbering the loader would give them."""

    header: tuple[str, ...]
    keys: list[tuple[int, int]]
    mini_csv: bytes
    segments: int
    bytes_read: int


def collect_range(header: Sequence[str], segments: Sequence[bytes]) -> RangeRows:
    """``occ`` from a dict over this range only, plus the range as a standalone canonical file.

    The header arrives as the staged cell list rather than as the canonical header line, because
    canonical bytes do not always re-parse: ``csv.writer(lineterminator="\\n")`` leaves a cell
    holding a bare ``\\r`` unquoted and ``csv.reader`` then rejects the line.
    """
    header = tuple(header)
    sink = io.StringIO()
    writer = csv.writer(sink, lineterminator="\n")
    writer.writerow(list(header))
    seen: dict[int, int] = {}
    keys: list[tuple[int, int]] = []
    for data in segments:
        for digest, cells in read_segment(data):
            occ = seen.get(digest, 0)
            seen[digest] = occ + 1
            keys.append((digest, occ))
            writer.writerow(cells)
    return RangeRows(
        header=header,
        keys=keys,
        mini_csv=sink.getvalue().encode("utf-8"),
        segments=len(segments),
        bytes_read=sum(len(data) for data in segments),
    )


def keys_digest(keys: Sequence[tuple[int, int]]) -> str:
    """One hash over every ``(row_hash, occ)`` pair in file order, to compare with the script."""
    hasher = hashlib.sha256()
    for digest, occ in keys:
        hasher.update(f"{digest}:{occ}\n".encode())
    return hasher.hexdigest()


async def _run_batches(
    db: Any, statements: Sequence[tuple[str, tuple[str, ...]]], *, per_batch: int
) -> int:
    calls = 0
    for start in range(0, len(statements), per_batch):
        group = statements[start : start + per_batch]
        await db.batch([db.prepare(sql).bind(*params) for sql, params in group])
        calls += 1
    return calls


@dataclass
class ValidatedRange:
    """What SQLModel validation produced for one range, with the cost of producing it."""

    rows: list[Any]
    rejects: list[Any]
    keys: list[tuple[int, int]]
    import_ms: int
    validate_ms: int

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.rejects)

    def as_report(self) -> dict[str, object]:
        return {
            "sqlmodel_import_ms": self.import_ms,
            "validate_ms": self.validate_ms,
            "validated_rows": len(self.rows),
            "validated_rejects": len(self.rejects),
            "validate_us_per_row": (
                int(self.validate_ms * 1000 / self.total) if self.total else None
            ),
        }


def validate_range(mini_csv: bytes, day: int) -> ValidatedRange:
    """Validate with the loader's SQLModel models, timing the lazy import and the validation.

    The import is lazy because ``roster_models`` pulls in sqlmodel, which ``early_vote_sync`` and
    the rest of the Worker must not load at import time (AGENTS.md, ADR-009). ADR-014 decision 5
    allows it here and asks the spike to measure it, which is what ``sqlmodel_import_ms`` is.
    """
    import_started = time.perf_counter()
    from texas_turnout_scraper.roster_models import parse_roster_batches

    import_ms = int((time.perf_counter() - import_started) * 1000)
    validate_started = time.perf_counter()
    rows: list[Any] = []
    rejects: list[Any] = []
    keys: list[tuple[int, int]] = []
    for batch in parse_roster_batches(mini_csv, day=day, added_in=_SCRATCH_VERSION):
        rows.extend(batch.rows)
        rejects.extend(batch.rejects)
        keys.extend((item.row_hash, item.occ) for item in (*batch.rows, *batch.rejects))
    return ValidatedRange(
        rows=rows,
        rejects=rejects,
        keys=keys,
        import_ms=import_ms,
        validate_ms=int((time.perf_counter() - validate_started) * 1000),
    )


async def write_range(
    bucket: StagingBucket,
    db: Any,
    request: StageRequest,
    range_index: int,
    *,
    verify: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, object]:
    """Read one range's segments, number duplicates, validate, and write to the scratch D1."""
    started = clock()
    staged_header = await bucket.read(request.header_key)
    if staged_header is None:
        return {"error": "no_staged_header", "header_key": request.header_key}
    header = [str(cell) for cell in json.loads(staged_header)]
    segment_keys = await bucket.keys(request.range_prefix(range_index))
    segments = [data for data in [await bucket.read(key) for key in segment_keys] if data]
    collected = collect_range(header, segments)
    read_ms = _elapsed_ms(clock, started)
    day = day_number(request.ev_date)

    report: dict[str, object] = {
        "range": range_index,
        "verify_only": verify,
        "segments_read": collected.segments,
        "segment_bytes_read": collected.bytes_read,
        "rows": len(collected.keys),
        "distinct_row_hashes": len({digest for digest, _ in collected.keys}),
        "max_occ": max((occ for _, occ in collected.keys), default=-1) + 1,
        "read_ms": read_ms,
        "keys_sha256": keys_digest(collected.keys),
        "first_keys": [list(key) for key in collected.keys[:_VERIFY_SAMPLE]],
        "last_keys": [list(key) for key in collected.keys[-_VERIFY_SAMPLE:]],
        "cpu_note": _CPU_NOTE,
    }
    if not collected.keys:
        return report

    try:
        validated = validate_range(collected.mini_csv, day)
    except (RosterSchemaError, csv.Error) as exc:
        # Canonical bytes are not always re-parsable (see collect_range). A real roster header
        # and cells hold no bare CR, so this is a reported dead end, not a fallback path.
        report["reparse_error"] = type(exc).__name__
        return report
    report.update(validated.as_report())
    report["validated_keys_match_scatter"] = sorted(validated.keys) == sorted(collected.keys)
    if verify:
        return report
    report.update(await _write_to_scratch(db, validated, day, clock=clock))
    return report


async def _write_to_scratch(
    db: Any, validated: ValidatedRange, day: int, *, clock: Callable[[], float]
) -> dict[str, object]:
    statements = [
        *insert_statements(day, _SCRATCH_VERSION, validated.rows),
        *reject_statements(day, _SCRATCH_VERSION, validated.rejects),
    ]
    payload_sizes = [len(params[-1].encode("utf-8")) for _, params in statements]
    out: dict[str, object] = {
        "statements": len(statements),
        "rows_per_statement": ROWS_PER_STATEMENT,
        "statements_per_batch": STATEMENTS_PER_BATCH,
        "max_bound_param_bytes": max(payload_sizes, default=0),
        "total_bound_param_bytes": sum(payload_sizes),
        "bound_param_accepted": None,
        "write_error": None,
    }
    started = clock()
    try:
        await db.batch([db.prepare(sql) for sql in ROW_SCHEMA_CREATES])
        calls = await _run_batches(db, statements, per_batch=STATEMENTS_PER_BATCH)
    except Exception as exc:  # noqa: BLE001 - report the class only, never the message
        out.update({"bound_param_accepted": False, "write_error": type(exc).__name__})
        return out
    written = validated.total
    write_ms = _elapsed_ms(clock, started)
    out.update(
        {
            "batch_calls": calls,
            "write_ms": write_ms,
            "rows_written": written,
            "rows_per_second": int(written * 1000 / write_ms) if write_ms else None,
            "bound_param_accepted": max(payload_sizes, default=0) > 0,
        }
    )
    return out


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=400, headers={"Cache-Control": "no-store"})


def _no_store(body: dict[str, object]) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


async def _stored_comparison(
    active: Any, request: StageRequest, canonical: object
) -> dict[str, object]:
    """``early_vote_day.sha256`` is over canonical bytes, so it compares with ``sha256``."""
    out: dict[str, object] = {"stored_sha256": None, "stored_matches_canonical": None}
    db = active.rosters.database(request.election_id)
    if db is None:
        out["stored_lookup"] = "no_binding"
        return out
    try:
        stored = await stored_day_hash(db, request.election_id, request.ev_date)
    except Exception as exc:  # noqa: BLE001 - report the class only
        out["stored_lookup"] = f"failed:{type(exc).__name__}"
        return out
    out["stored_sha256"] = stored
    if stored is not None and isinstance(canonical, str):
        out["stored_matches_canonical"] = stored == canonical
    return out


class _StageApi:
    """Route handlers. ``staging`` and ``scratch`` override the bindings so tests use fakes."""

    def __init__(
        self,
        deps_for_request: Callable[[Request], Any],
        *,
        fetch: Fetch | None,
        clock: Callable[[], float],
        today: Callable[[], date],
        staging: StagingBucket | None,
        scratch: Any | None,
    ) -> None:
        self._deps = deps_for_request
        self._fetch = fetch or _workers_fetch
        self._today = today
        self._staging = staging
        self._scratch = scratch
        self._gate = _CallGate(clock)

    async def _authorized(self, request: Request) -> Any | None:
        active = self._deps(request)
        if not await read_request_authorized(
            request.headers,
            access_client_id=active.access_client_id,
            access_client_secret=active.access_client_secret,
            kv=active.kv,
            token_db=active.token_db,
        ):
            return None
        return active

    def _bucket(self, request: Request) -> StagingBucket | None:
        if self._staging is not None:
            return self._staging
        binding = getattr(request.scope.get("env"), "STAGING", None)
        return None if binding is None else R2StagingBucket(binding)

    def _scratch_db(self, request: Request) -> Any | None:
        if self._scratch is not None:
            return self._scratch
        return getattr(request.scope.get("env"), "SCRATCH", None)

    async def stage(
        self,
        request: Request,
        election_id: str | None = Query(default=None),
        ev_date: str | None = Query(default=None, alias="date"),
        suffix: str | None = Query(default=None),
        cleanup: str | None = Query(default=None),
    ) -> Response:
        active = await self._authorized(request)
        if active is None:
            return Response(status_code=401)
        try:
            parsed = parse_stage_request(election_id, ev_date, suffix, today=self._today())
        except SpikeRequestError as exc:
            return _bad_request(exc.detail)
        bucket = self._bucket(request)
        if bucket is None:
            return _no_store({"error": "no_staging_binding"})
        if cleanup == "1":
            return _no_store(await cleanup_run(bucket, parsed))
        retry_after = self._gate.claim()
        if retry_after is not None:
            return JSONResponse(
                {"detail": "rate_limited"},
                status_code=429,
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        report = await stage_day(
            self._fetch, portal_url(parsed.election_id, parsed.day), bucket, parsed
        )
        report.update({"election_id": parsed.election_id, "ev_date": parsed.ev_date})
        report.update(await _stored_comparison(active, parsed, report.get("sha256")))
        return _no_store(report)

    async def range_write(
        self,
        request: Request,
        election_id: str | None = Query(default=None),
        ev_date: str | None = Query(default=None, alias="date"),
        suffix: str | None = Query(default=None),
        hash_range: str | None = Query(default=None, alias="range"),
        verify: str | None = Query(default=None),
    ) -> Response:
        if await self._authorized(request) is None:
            return Response(status_code=401)
        try:
            parsed = parse_stage_request(election_id, ev_date, suffix, today=self._today())
            index = checked_range(hash_range)
        except SpikeRequestError as exc:
            return _bad_request(exc.detail)
        bucket = self._bucket(request)
        if bucket is None:
            return _no_store({"error": "no_staging_binding"})
        db = self._scratch_db(request)
        if db is None and verify != "1":
            return _no_store({"error": "no_scratch_binding"})
        return _no_store(await write_range(bucket, db, parsed, index, verify=verify == "1"))


def register_stage_routes(
    app: FastAPI,
    deps_for_request: Callable[[Request], Any],
    *,
    fetch: Fetch | None = None,
    clock: Callable[[], float] = time.monotonic,
    today: Callable[[], date] = _utc_today,
    staging: StagingBucket | None = None,
    scratch: Any | None = None,
) -> None:
    api = _StageApi(
        deps_for_request,
        fetch=fetch,
        clock=clock,
        today=today,
        staging=staging,
        scratch=scratch,
    )
    app.add_api_route("/spike/stage", api.stage, methods=["GET"], response_model=None)
    app.add_api_route("/spike/range-write", api.range_write, methods=["POST"], response_model=None)

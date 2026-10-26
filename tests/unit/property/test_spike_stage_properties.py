"""Differential properties for the spike's streaming canonicalizer and hash-range scatter.

The reference is ``canonicalize_roster_csv`` plus ``keyed_lines`` over the whole file. The
reference rejects some files outright (a lone CR inside an unquoted field raises ``csv.Error``),
so the properties below pin that the streaming version accepts and rejects exactly the same
files. Every generated file is synthetic; a falsifying example never holds a real voter.
"""

from __future__ import annotations

import csv
import hashlib
import io
from itertools import pairwise

import pytest
from ev_api.spike_stage import (
    RowScatter,
    StreamingCanonicalizer,
    collect_range,
    header_seed,
    keys_digest,
    range_of,
    row_hash,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from texas_turnout_scraper.early_vote_roster import canonicalize_roster_csv
from texas_turnout_scraper.early_vote_roster_rows import KEY_RANGES, key_ranges, keyed_lines

# Cell text the reference accepts: the awkward separators a naive splitlines() would break on,
# the characters csv has to quote, and the BOM character itself inside a cell.
_AWKWARD = "\r\x0b\x0c\x85" + chr(0x2028) + chr(0x2029) + '\ufeff",\n\t '
_CELL = st.text(alphabet=st.sampled_from([*_AWKWARD, "a", "B", "0", "é", "名"]), max_size=6)
_ROW = st.lists(_CELL, max_size=4)
_LINE_ENDING = st.sampled_from(["\n", "\r\n", "\r", ""])


def _serialize(rows: list[list[str]], endings: list[str], *, bom: bool) -> bytes:
    """A CSV-ish file built by hand, so quoting and line endings are the generator's choice."""
    pieces = []
    for row, ending in zip(rows, endings, strict=True):
        cells = []
        for cell in row:
            quoted = any(character in cell for character in ',"\n\r')
            cells.append('"' + cell.replace('"', '""') + '"' if quoted else cell)
        pieces.append(",".join(cells) + ending)
    return (("﻿" if bom else "") + "".join(pieces)).encode("utf-8")


@st.composite
def roster_bytes(draw: st.DrawFn) -> bytes:
    rows = draw(st.lists(_ROW, max_size=8))
    endings = draw(st.lists(_LINE_ENDING, min_size=len(rows), max_size=len(rows)))
    return _serialize(rows, endings, bom=draw(st.booleans()))


@st.composite
def chunked(draw: st.DrawFn) -> tuple[bytes, list[bytes]]:
    data = draw(roster_bytes())
    cuts = sorted(draw(st.lists(st.integers(min_value=0, max_value=len(data)), max_size=12)))
    points = [0, *cuts, len(data)]
    return data, [data[a:b] for a, b in pairwise(points) if b > a]


def _records(pieces: list[bytes]) -> list[object]:
    machine = StreamingCanonicalizer()
    out = [record for piece in pieces for record in machine.feed(piece)]
    out.extend(machine.finish())
    return out


def _stream(pieces: list[bytes]) -> bytes:
    return b"".join(record.canonical for record in _records(pieces))  # type: ignore[attr-defined]


def _reference(data: bytes) -> bytes | None:
    """The reference canonical bytes, or None when the reference rejects the file."""
    try:
        return canonicalize_roster_csv(data)[0]
    except csv.Error:
        return None


def _reference_keys(data: bytes) -> list[tuple[tuple[int, int], tuple[str, ...]]] | None:
    try:
        return [(key, tuple(cells)) for key, cells in keyed_lines(data)]
    except csv.Error:
        return None


@settings(max_examples=400)
@given(chunked())
def test_streaming_canonical_bytes_equal_the_reference(case: tuple[bytes, list[bytes]]) -> None:
    data, pieces = case
    expected = _reference(data)
    if expected is None:
        with pytest.raises(csv.Error):
            _stream(pieces)
        return
    assert _stream(pieces) == expected


@given(roster_bytes())
def test_byte_at_a_time_and_one_shot_agree_with_the_reference(data: bytes) -> None:
    expected = _reference(data)
    if expected is None:
        return
    assert _stream([data[index : index + 1] for index in range(len(data))]) == expected
    assert _stream([data] if data else []) == expected


@given(chunked())
def test_streamed_rows_are_the_rows_keyed_lines_sees(case: tuple[bytes, list[bytes]]) -> None:
    data, pieces = case
    expected = _reference_keys(data)
    if expected is None:
        return
    streamed = [record.cells for record in _records(pieces) if not record.is_header]
    assert streamed == [cells for _, cells in expected]


@given(roster_bytes())
def test_row_hash_matches_keyed_lines(data: bytes) -> None:
    expected = _reference_keys(data)
    records = _records([data]) if expected is not None else []
    if not records:
        return
    seed = header_seed(records[0].cells)
    streamed = [row_hash(seed, record.cells) for record in records if not record.is_header]
    assert streamed == [digest for (digest, _), _ in expected]


def _scatter_segments(
    records: list[object], *, flush_bytes: int
) -> tuple[RowScatter, list[list[bytes]]]:
    header, *rows = records
    scatter = RowScatter(header.cells, flush_bytes=flush_bytes)
    per_range: list[list[bytes]] = [[] for _ in range(KEY_RANGES)]
    for record in rows:
        _, segment = scatter.add(record.cells)
        if segment is not None:
            per_range[segment.range_index].append(segment.data)
    for segment in scatter.flush_all():
        per_range[segment.range_index].append(segment.data)
    return scatter, per_range


@given(chunked(), st.integers(min_value=1, max_value=200))
def test_scatter_plus_per_range_occ_reproduces_keyed_lines(
    case: tuple[bytes, list[bytes]], flush_bytes: int
) -> None:
    """Every ``(row_hash, occ)`` the loader computes, rebuilt one range at a time."""
    data, pieces = case
    expected = _reference_keys(data)
    records = _records(pieces) if expected is not None else []
    if not records:
        return
    scatter, per_range = _scatter_segments(records, flush_bytes=flush_bytes)
    rebuilt: list[tuple[int, int]] = []
    for index in range(KEY_RANGES):
        collected = collect_range(records[0].cells, per_range[index])
        low, high = (int(bound) for bound in key_ranges()[index])
        assert all(low <= digest <= high for digest, _ in collected.keys)
        assert len(collected.keys) == scatter.rows_per_range[index]
        assert len(per_range[index]) == scatter.segments_per_range[index]
        rebuilt.extend(collected.keys)
    assert sorted(rebuilt) == sorted(key for key, _ in expected)
    assert sum(scatter.rows_per_range) == len(expected)


@given(roster_bytes())
def test_duplicate_lines_get_the_same_occ_the_loader_gives_them(data: bytes) -> None:
    """Repeating the body makes every key a duplicate, which is where per-range occ could drift."""
    body = data[data.find(b"\n") + 1 :] if b"\n" in data else b""
    doubled = data + body + body
    expected = _reference_keys(doubled)
    records = _records([doubled]) if expected is not None else []
    if not records or len(records) == 1:
        return
    _, per_range = _scatter_segments(records, flush_bytes=1)
    rebuilt = [
        key
        for index in range(KEY_RANGES)
        for key in collect_range(records[0].cells, per_range[index]).keys
    ]
    assert sorted(rebuilt) == sorted(key for key, _ in expected)
    assert max((occ for _, occ in rebuilt), default=-1) == max(
        (occ for (_, occ), _ in expected), default=-1
    )


@given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_range_of_agrees_with_key_ranges_bounds(value: int) -> None:
    low, high = (int(bound) for bound in key_ranges()[range_of(value)])
    assert low <= value <= high


def test_keys_digest_is_order_sensitive() -> None:
    assert keys_digest([(1, 0), (2, 0)]) != keys_digest([(2, 0), (1, 0)])
    assert keys_digest([(1, 0)]) == hashlib.sha256(b"1:0\n").hexdigest()


def test_reference_canonical_bytes_do_not_always_re_parse() -> None:
    """Why the staged header is JSON and ``collect_range`` never parses canonical bytes.

    ``csv.writer(lineterminator="\\n")`` leaves a cell holding a bare CR unquoted, and
    ``csv.reader`` rejects a bare CR in an unquoted field. Any pipeline step that re-reads the
    canonical object therefore cannot round-trip such a file.
    """
    canonical, _ = canonicalize_roster_csv(b'"\r0"\n1\n')
    assert canonical == b"\r0\n1\n"
    with pytest.raises(csv.Error):
        list(csv.reader(io.StringIO(canonical.decode("utf-8-sig"))))
    assert collect_range(["\r0"], []).header == ("\r0",)

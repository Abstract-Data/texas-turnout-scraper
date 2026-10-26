"""Stateful properties of the roster row store and the repeat build, each against a plain model.

``RosterStoreMachine`` loads generated day files through ``load_roster_day`` into sqlite with the
real DDL. Its model is the latest file of each day. After every step the rows visible at each
day's marker equal the multiset the model derives from the file; version ranges never invert;
an unchanged reload writes only the marker; readers of the old version see the old file until
the marker flips and nothing after the flip changes what the new version serves; a load that
crashed at any statement converges, when re-run, to what a fresh store holds for the same file.

``RepeatBuildMachine`` adds and removes live roster rows (with and without a change-log event)
and rebuilds. After a build the repeat tables equal an oracle computed in SQL with window
functions; ``raw - excess == distinct``; an unchanged signature sends no statement.

All data is synthetic: names are fake tokens and VUIDs are generated digit strings.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    precondition,
    rule,
)

pytest.importorskip("sqlmodel")

from texas_turnout_scraper.early_vote_backfill import (  # noqa: E402
    plan_backfill_load_dates,
    run_backfill,
)
from texas_turnout_scraper.early_vote_day_changes import DAY_CHANGE_TABLE_SQL  # noqa: E402
from texas_turnout_scraper.early_vote_roster_rows import (  # noqa: E402
    ROW_SCHEMA_CREATES,
    ROW_TABLE_SQL,
    VERSION_TABLE_SQL,
    VISIBLE_AT_SQL,
    RosterSchemaError,
    day_number,
)
from texas_turnout_scraper.repeat_build import run_repeat_build_on_store  # noqa: E402
from texas_turnout_scraper.repeat_tables import (  # noqa: E402
    FLAG_CROSS_DAY,
    FLAG_CROSS_METHOD,
    FLAG_REMOVED,
    FLAG_SAME_DAY,
    REPEAT_SCHEMA_CREATES,
)
from texas_turnout_scraper.roster_models import load_roster_day  # noqa: E402

ELECTION = "53815"
DB = "db"

# A leap day, both sides of a year boundary, and two adjacent October days.
EV_DATES = ("2026-10-19", "2026-10-20", "2024-02-29", "2026-12-31", "2027-01-01")
DAY_OF = {ev_date: day_number(ev_date) for ev_date in EV_DATES}
DATE_OF = {day: ev_date for ev_date, day in DAY_OF.items()}

CANON = ("COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT", "POLL_PLACE_NAME")
KNOWN = {"COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT"}
# Headers as a county might write them: canonical, reordered with odd case and padding, and one
# without the optional PRECINCT column.
HEADERS: tuple[tuple[str, ...], ...] = (
    CANON,
    ("ID_VOTER", " county ", "Voter_Name", "PRECINCT", "POLL_PLACE_NAME", "voting_method"),
    ("COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "POLL_PLACE_NAME"),
)
BAD_HEADERS: tuple[tuple[str, ...], ...] = (
    (),
    ("COUNTY", "VOTER_NAME", "VOTING_METHOD"),
    ("COUNTY", "county ", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD"),
)


def _canon(name: str) -> str:
    return name.strip().upper()


# Cells. Each pool mixes valid values, values that only normalize to valid, and invalid ones.
_FAKE_NAMES = (
    "ANNA ALPHA",
    "BRAVO, BOB",
    'CHARLIE "CHUCK" CARL',
    "DORA\nDELTA",
    "ÉCHO ÉLAN",
    "FOXTROT FÉLIX",
    "﻿GOLF GHOST",
    "HOTEL\r\nHANK",
    "  ",
    "",
)
_cell_alphabet = st.sampled_from(list("AZaz09 ,\"'\n\r\t-é́﻿"))
fake_names = st.one_of(st.sampled_from(_FAKE_NAMES), st.text(_cell_alphabet, max_size=6))
vuids = st.one_of(
    st.text("0123456789", min_size=1, max_size=10),
    st.sampled_from(
        (
            "0000012345",
            "12345",
            " 12345 ",
            "0",
            "9999999999",
            "",
            " ",
            "00000000000",
            "12a45",
            "١٢٣",
            "²",
            "-1",
        )
    ),
)
methods = st.sampled_from(
    ("IN-PERSON", "MAIL-IN", "in-person", " Mail-In ", "EARLY", "", "IN PERSON")
)
counties = st.sampled_from(("TRAVIS", "HARRIS", "TRAVIS ", "Béxar", "  ", ""))
precincts = st.sampled_from(("", "101", "0101", " 7"))
polls = st.text(_cell_alphabet, max_size=5)


@dataclass(frozen=True)
class Record:
    """One data line in canonical column order; ``ragged`` cells are cut (<0) or added (>0)."""

    cells: tuple[str, ...]
    ragged: int = 0


records = st.builds(
    Record,
    st.tuples(counties, fake_names, vuids, methods, precincts, polls),
    st.sampled_from((0, 0, 0, 0, -1, -5, 1, 2)),
)


@dataclass(frozen=True)
class DayFile:
    header: tuple[str, ...]
    records: tuple[Record, ...]


@dataclass(frozen=True)
class Format:
    bom: bool
    crlf: bool
    quote_all: bool
    trailing_blank: bool


formats = st.builds(Format, st.booleans(), st.booleans(), st.booleans(), st.booleans())


@st.composite
def multisets(draw: st.DrawFn, *, max_size: int = 10) -> tuple[Record, ...]:
    """Lines drawn from a small pool, so identical lines (one key, several ``occ``) are common."""
    pool = draw(st.lists(records, min_size=1, max_size=4))
    return tuple(draw(st.lists(st.sampled_from(pool), max_size=max_size)))


day_files = st.builds(DayFile, st.sampled_from(HEADERS), multisets())


def _line(header: Sequence[str], record: Record) -> list[str]:
    by_name = dict(zip(CANON, record.cells, strict=True))
    cells = [by_name[_canon(name)] for name in header]
    if record.ragged < 0:
        return cells[: max(1, len(cells) + record.ragged)]
    return cells + [f"spill{i}" for i in range(record.ragged)]


def render(day_file: DayFile, fmt: Format) -> bytes:
    out = io.StringIO()
    lines = [_line(day_file.header, record) for record in day_file.records]
    # With a "\n" terminator csv.writer leaves a lone "\r" unquoted, which no reader can tell
    # from a line break, so such a file is quoted in full.
    bare_cr = any("\r" in cell for cells in lines for cell in cells)
    writer = csv.writer(
        out,
        lineterminator="\r\n" if fmt.crlf else "\n",
        quoting=csv.QUOTE_ALL if fmt.quote_all or bare_cr else csv.QUOTE_MINIMAL,
    )
    writer.writerow(day_file.header)
    writer.writerows(lines)
    text = out.getvalue() + ("\r\n" if fmt.crlf else "\n") * fmt.trailing_blank
    return (b"\xef\xbb\xbf" if fmt.bom else b"") + text.encode("utf-8")


def _canon_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _vuid_ok(text: str) -> bool:
    return text.isascii() and text.isdigit() and len(text) <= 10


def expected(day_file: DayFile | None) -> tuple[Counter[tuple[str, ...]], Counter[str]]:
    """Rows and rejects the requirement says a day file yields, written independently of the code.

    A line whose cell count differs from the header is rejected whole; otherwise it is a row when
    COUNTY and VOTER_NAME are not blank, ID_VOTER is at most ten ASCII digits after a strip
    (padded to ten) and VOTING_METHOD is IN-PERSON or MAIL-IN after strip and upper-case. Every
    other column lands in ``extra`` under the header as written.
    """
    rows: Counter[tuple[str, ...]] = Counter()
    rejects: Counter[str] = Counter()
    if day_file is None:
        return rows, rejects
    header = day_file.header
    for record in day_file.records:
        cells = _line(header, record)
        raw: dict[str, object] = dict(zip(header, cells, strict=False))
        if len(cells) != len(header):
            if len(cells) > len(header):
                raw["_overflow"] = cells[len(header) :]
            rejects[_canon_json(raw)] += 1
            continue
        known = {_canon(h): c for h, c in zip(header, cells, strict=True) if _canon(h) in KNOWN}
        extra = {h: c for h, c in zip(header, cells, strict=True) if _canon(h) not in KNOWN}
        vuid = known["ID_VOTER"].strip()
        method = known["VOTING_METHOD"].strip().upper()
        if (
            known["COUNTY"].strip()
            and known["VOTER_NAME"].strip()
            and _vuid_ok(vuid)
            and method in {"IN-PERSON", "MAIL-IN"}
        ):
            rows[
                (
                    known["COUNTY"],
                    known["VOTER_NAME"],
                    vuid.zfill(10),
                    method,
                    known.get("PRECINCT", ""),
                    _canon_json(extra),
                )
            ] += 1
        else:
            rejects[_canon_json(raw)] += 1
    return rows, rejects


def _same_lines(a: DayFile, b: DayFile) -> bool:
    def lines(day_file: DayFile) -> Counter[tuple[str, ...]]:
        return Counter(tuple(_line(day_file.header, r)) for r in day_file.records)

    # Keys hash the header with each line, so two files without data lines share the empty key set.
    return lines(a) == lines(b) and (a.header == b.header or not a.records)


class _Crash(Exception):
    pass


@dataclass(frozen=True)
class Marker:
    version: int
    file_id: str
    rows: int
    rejects: int


class _RowStore:
    """The loader's two store primitives on sqlite with the real DDL.

    Before the marker upsert runs it records what a reader of the old and of the new version
    would see, and ``crash_at`` raises before the statement with that index.
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in ROW_SCHEMA_CREATES:
            self.conn.execute(sql)
        self.sent: list[str] = []
        self.flips: list[tuple[int, int, Any, Any]] = []
        self.crash_at: int | None = None

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            if self.crash_at is not None and len(self.sent) == self.crash_at:
                raise _Crash
            if sql.startswith("INSERT INTO early_vote_roster_version"):
                ev_date, new = params[1], int(params[2])
                old = self.marker(ev_date)
                old_version = 0 if old is None else old.version
                day = DAY_OF[ev_date]
                self.flips.append(
                    (old_version, new, self.visible(day, old_version), self.visible(day, new))
                )
            self.sent.append(sql)
            self.conn.execute(sql, params)

    def marker(self, ev_date: str) -> Marker | None:
        row = self.conn.execute(
            "SELECT version, file_id, row_count, reject_count FROM early_vote_roster_version "
            "WHERE source_election_id = ? AND ev_date = ?",
            (ELECTION, ev_date),
        ).fetchone()
        return None if row is None else Marker(*row)

    def visible(self, day: int, version: int) -> tuple[Counter[tuple[str, ...]], Counter[str]]:
        params = (day, version, version)
        rows = Counter(
            (county, name, vuid, method, precinct, _canon_json(json.loads(extra)))
            for county, name, vuid, method, precinct, extra in self.conn.execute(
                "SELECT county, voter_name, id_voter, voting_method, precinct, extra "
                f"FROM early_vote_roster_row WHERE day = ? AND {VISIBLE_AT_SQL}",
                params,
            )
        )
        rejects = Counter(
            _canon_json(json.loads(raw))
            for (raw,) in self.conn.execute(
                f"SELECT raw FROM early_vote_roster_reject WHERE day = ? AND {VISIBLE_AT_SQL}",
                params,
            )
        )
        return rows, rejects

    def physical(self, day: int) -> int:
        return sum(
            self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE day = ?", (day,)).fetchone()[0]
            for table in ("early_vote_roster_row", "early_vote_roster_reject")
        )

    def load(
        self, ev_date: str, decoded: bytes, file_id: str, load_base: Callable[[str], bytes | None]
    ) -> Any:
        self.sent.clear()
        self.flips.clear()
        return load_roster_day(
            self,
            DB,
            ELECTION,
            ev_date,
            file_id=file_id,
            sha256=f"sha-{file_id}",
            decoded=decoded,
            load_base=load_base,
        )


def _total(counts: tuple[Counter[Any], Counter[Any]]) -> int:
    return sum(counts[0].values()) + sum(counts[1].values())


class RosterStoreMachine(RuleBasedStateMachine):
    interrupted: Bundle[tuple[str, DayFile]] = Bundle("interrupted")

    def __init__(self) -> None:
        super().__init__()
        self.store = _RowStore()
        self.files: dict[str, bytes] = {}
        self.model: dict[str, DayFile] = {}
        self.versions: dict[str, int] = {}
        self.next_id = 0

    def _file_id(self, decoded: bytes) -> str:
        self.next_id += 1
        file_id = f"f{self.next_id}"
        self.files[file_id] = decoded
        return file_id

    def _base(self, lose_base: bool) -> Callable[[str], bytes | None]:
        return (lambda _file_id: None) if lose_base else self.files.get

    def _load(self, ev_date: str, day_file: DayFile, fmt: Format, *, lose_base: bool) -> None:
        """A load that runs to the end, with the checks every successful load must pass."""
        previous = self.model.get(ev_date)
        old = expected(previous)
        new = expected(day_file)
        decoded = render(day_file, fmt)
        result = self.store.load(ev_date, decoded, self._file_id(decoded), self._base(lose_base))
        unchanged = previous is not None and not lose_base and _same_lines(previous, day_file)
        if unchanged:
            assert len(self.store.sent) == 1, "an unchanged reload writes only the marker"
            assert (result.added, result.removed) == (0, 0)
            assert result.version == self.versions[ev_date]
        else:
            assert result.version == self.versions.get(ev_date, 0) + 1
            assert self.store.physical(DAY_OF[ev_date]) == _total(new), "nothing left behind"
        assert len(self.store.flips) == 1
        old_version, new_version, at_old, at_new = self.store.flips[0]
        assert new_version == result.version
        assert at_old == old, "old readers see the old file until the flip"
        assert at_new == new, "the new version is complete before the flip"
        self.model[ev_date] = day_file
        self.versions[ev_date] = result.version

    @initialize(ev_date=st.sampled_from(EV_DATES), day_file=day_files, fmt=formats)
    def first_load(self, ev_date: str, day_file: DayFile, fmt: Format) -> None:
        self._load(ev_date, day_file, fmt, lose_base=False)

    @rule(
        ev_date=st.sampled_from(EV_DATES),
        day_file=day_files,
        fmt=formats,
        lose_base=st.booleans(),
    )
    def load_day(self, ev_date: str, day_file: DayFile, fmt: Format, lose_base: bool) -> None:
        self._load(ev_date, day_file, fmt, lose_base=lose_base)

    @precondition(lambda self: bool(self.model))
    @rule(data=st.data(), fmt=formats)
    def reload_identical(self, data: st.DataObject, fmt: Format) -> None:
        ev_date = data.draw(st.sampled_from(sorted(self.model)))
        current = self.model[ev_date]
        shuffled = data.draw(st.permutations(current.records))
        self._load(ev_date, DayFile(current.header, tuple(shuffled)), fmt, lose_base=False)

    @precondition(lambda self: bool(self.model))
    @rule(data=st.data(), fmt=formats)
    def reload_changed(self, data: st.DataObject, fmt: Format) -> None:
        ev_date = data.draw(st.sampled_from(sorted(self.model)))
        current = self.model[ev_date]
        kept = list(data.draw(st.permutations(current.records)))
        kept = kept[data.draw(st.integers(0, len(kept))) :]
        repeated = data.draw(st.lists(st.sampled_from(kept), max_size=3)) if kept else []
        added = data.draw(st.lists(records, max_size=3))
        lines = data.draw(st.permutations([*kept, *repeated, *added]))
        self._load(ev_date, DayFile(current.header, tuple(lines)), fmt, lose_base=False)

    @rule(ev_date=st.sampled_from(EV_DATES), header=st.sampled_from(HEADERS), fmt=formats)
    def load_empty(self, ev_date: str, header: tuple[str, ...], fmt: Format) -> None:
        self._load(ev_date, DayFile(header, ()), fmt, lose_base=False)

    @rule(
        ev_date=st.sampled_from(EV_DATES),
        header=st.sampled_from(BAD_HEADERS),
        lines=st.lists(records, max_size=3),
        fmt=formats,
    )
    def load_bad_header(
        self, ev_date: str, header: tuple[str, ...], lines: list[Record], fmt: Format
    ) -> None:
        out = io.StringIO()
        writer = csv.writer(out)
        if header:
            writer.writerow(header)
            writer.writerows(record.cells for record in lines)
        decoded = out.getvalue().encode()
        with pytest.raises(RosterSchemaError) as caught:
            self.store.load(ev_date, decoded, self._file_id(decoded), self.files.get)
        assert self.store.sent == [], "a bad header stops before any write"
        for record in lines:
            for cell in record.cells:
                if len(cell.strip()) > 2:
                    assert cell not in str(caught.value)

    @rule(
        target=interrupted,
        ev_date=st.sampled_from(EV_DATES),
        day_file=day_files,
        fmt=formats,
        crash_at=st.integers(0, 130),
    )
    def crash_mid_load(
        self, ev_date: str, day_file: DayFile, fmt: Format, crash_at: int
    ) -> tuple[str, DayFile]:
        previous = self.model.get(ev_date)
        decoded = render(day_file, fmt)
        self.store.crash_at = crash_at
        try:
            result = self.store.load(ev_date, decoded, self._file_id(decoded), self.files.get)
        except _Crash:
            result = None
        finally:
            self.store.crash_at = None
        if result is not None or self.store.flips:
            # The marker was written: that version is published even if collection never ran.
            marker = self.store.marker(ev_date)
            assert marker is not None
            self.model[ev_date] = day_file
            self.versions[ev_date] = marker.version
        else:
            marker = self.store.marker(ev_date)
            assert (marker is None) == (previous is None)
            assert self.versions.get(ev_date, 0) == (0 if marker is None else marker.version)
        return ev_date, day_file

    @rule(attempt=consumes(interrupted), fmt=formats)
    def rerun_after_crash(self, attempt: tuple[str, DayFile], fmt: Format) -> None:
        ev_date, day_file = attempt
        self._load(ev_date, day_file, fmt, lose_base=False)
        fresh = _RowStore()
        decoded = render(day_file, fmt)
        fresh.load(ev_date, decoded, "fresh", {"fresh": decoded}.get)
        mine, theirs = self.store.marker(ev_date), fresh.marker(ev_date)
        assert mine is not None and theirs is not None
        day = DAY_OF[ev_date]
        assert self.store.visible(day, mine.version) == fresh.visible(day, theirs.version)
        assert (mine.rows, mine.rejects) == (theirs.rows, theirs.rejects)

    @invariant()
    def live_rows_equal_the_model(self) -> None:
        for ev_date in EV_DATES:
            marker = self.store.marker(ev_date)
            if ev_date not in self.model:
                assert marker is None
                continue
            assert marker is not None
            assert marker.version == self.versions[ev_date]
            rows, rejects = expected(self.model[ev_date])
            assert self.store.visible(DAY_OF[ev_date], marker.version) == (rows, rejects)
            assert (marker.rows, marker.rejects) == (sum(rows.values()), sum(rejects.values()))

    @invariant()
    def version_ranges_never_invert(self) -> None:
        for table in ("early_vote_roster_row", "early_vote_roster_reject"):
            inverted = self.store.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE added_in < 1 "
                "OR (removed_in IS NOT NULL AND removed_in <= added_in)"
            ).fetchone()[0]
            assert inverted == 0
            for day, newest in self.store.conn.execute(
                f"SELECT day, MAX(MAX(added_in), MAX(COALESCE(removed_in, 0))) FROM {table} "
                "GROUP BY day"
            ):
                published = self.versions.get(DATE_OF[day], 0)
                assert newest <= published + 1, "at most one unpublished attempt is pending"


RosterStoreMachine.TestCase.settings = settings(
    max_examples=max(5, settings.default.max_examples // 2),
    stateful_step_count=min(20, settings.default.stateful_step_count),
)
TestRosterStore = RosterStoreMachine.TestCase


# Repeat build.

REPEAT_DATES = ("2024-02-29", "2026-10-20", "2026-10-21", "2026-12-31", "2027-01-01")
for _ev_date in REPEAT_DATES:
    DATE_OF.setdefault(day_number(_ev_date), _ev_date)
REPEAT_METHODS = ("IN-PERSON", "MAIL-IN")
REPEAT_COUNTIES = ("TRAVIS", "HARRIS", "BÉXAR")
# Short and space-padded forms of one VUID are the same voter once padded to ten digits.
repeat_vuids = st.one_of(
    st.sampled_from(("0000012345", "12345", " 12345", "12345 ", "9999999999", "0000000000", "0")),
    st.text("0123456789", min_size=1, max_size=10),
)
row_hashes = st.one_of(
    st.integers(-(2**63), 2**63 - 1),
    st.sampled_from((-(2**63), -1, 0, 1, 2**63 - 1)),
)
roster_rows = st.tuples(
    repeat_vuids, st.sampled_from(REPEAT_METHODS), st.sampled_from(REPEAT_COUNTIES), row_hashes
)

_LIVE_KEY = "substr('0000000000' || trim(id_voter), -10)"
_RANKED_SQL = (
    f"WITH live AS (SELECT {_LIVE_KEY} AS v, day, row_hash, occ, county, voting_method "
    "FROM early_vote_roster_row WHERE removed_in IS NULL), "
    "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY v ORDER BY day, row_hash, occ) AS rn "
    "FROM live) "
)


class _RepeatStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in (ROW_TABLE_SQL, VERSION_TABLE_SQL, DAY_CHANGE_TABLE_SQL):
            self.conn.execute(sql)
        self.statements: list[str] = []

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return DB

    def _ensure_schema(self, database_id: str) -> None:
        for sql in REPEAT_SCHEMA_CREATES:
            self.conn.execute(sql)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.statements.append(sql)
            self.conn.execute(sql, params)

    def rows(self, sql: str, params: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql, params).fetchall()


class RepeatBuildMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.store = _RepeatStore()
        self.occ: Counter[tuple[int, int]] = Counter()
        self.events = 0
        self.built = False
        self.stale = True

    def _bump(self, ev_date: str) -> None:
        self.store.conn.execute(
            "INSERT INTO early_vote_roster_version VALUES (?, ?, 1, 'f', 's', 0, 0) "
            "ON CONFLICT(source_election_id, ev_date) DO UPDATE SET version = version + 1, "
            "sha256 = sha256 || 'x'",
            (ELECTION, ev_date),
        )
        self.stale = True

    def _version(self, ev_date: str) -> int:
        return int(
            self.store.rows(
                "SELECT version FROM early_vote_roster_version WHERE ev_date = ?", (ev_date,)
            )[0][0]
        )

    def _live(self) -> list[tuple[int, int, int, str]]:
        return self.store.rows(
            "SELECT day, row_hash, occ, id_voter FROM early_vote_roster_row "
            "WHERE removed_in IS NULL ORDER BY day, row_hash, occ"
        )

    @rule(ev_date=st.sampled_from(REPEAT_DATES), rows=st.lists(roster_rows, max_size=6))
    def add_day_rows(self, ev_date: str, rows: list[tuple[str, str, str, int]]) -> None:
        self._bump(ev_date)
        day, version = day_number(ev_date), self._version(ev_date)
        for vuid, method, county, row_hash in rows:
            occ = self.occ[(day, row_hash)]
            self.occ[(day, row_hash)] += 1
            self.store.conn.execute(
                "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
                "precinct, day, row_hash, occ, extra, added_in, removed_in) "
                "VALUES (?, 'FAKE PERSON', ?, ?, '1', ?, ?, ?, '{}', ?, NULL)",
                (county, vuid, method, day, row_hash, occ, version),
            )

    @precondition(lambda self: bool(self._live()))
    @rule(data=st.data(), with_event=st.booleans())
    def remove_rows(self, data: st.DataObject, with_event: bool) -> None:
        picks = data.draw(
            st.lists(st.sampled_from(self._live()), min_size=1, max_size=3, unique=True)
        )
        for day, row_hash, occ, vuid in picks:
            ev_date = DATE_OF[day]
            self._bump(ev_date)
            self.store.conn.execute(
                "UPDATE early_vote_roster_row SET removed_in = ? "
                "WHERE day = ? AND row_hash = ? AND occ = ?",
                (self._version(ev_date), day, row_hash, occ),
            )
            if with_event:
                self.events += 1
                self.store.conn.execute(
                    "INSERT INTO early_vote_day_change VALUES (?, ?, 'old', ?, 'removed', ?, "
                    "'TRAVIS', 'IN-PERSON', ?)",
                    (ELECTION, ev_date, f"new{self.events}", vuid, f"t{self.events:06d}"),
                )

    @rule()
    def build(self) -> None:
        self.store.statements.clear()
        result = run_repeat_build_on_store(self.store, ELECTION, dry_run=False)
        assert result is not None
        if self.built and not self.stale:
            assert result.status == "unchanged"
            assert self.store.statements == [], "an unchanged signature sends nothing"
        else:
            assert result.status == "wrote"
            assert result.raw_rows - result.excess_rows == result.distinct_voters
        self.built, self.stale = True, False

    @rule()
    def build_twice(self) -> None:
        self.build()
        self.build()

    def _oracle_excess(self) -> dict[tuple[str, str, str], int]:
        return {
            (DATE_OF[day], county, method): n
            for day, county, method, n in self.store.rows(
                _RANKED_SQL + "SELECT day, county, voting_method, COUNT(*) FROM ranked "
                "WHERE rn > 1 GROUP BY day, county, voting_method"
            )
        }

    def _oracle_voters(self) -> dict[str, dict[str, Any]]:
        voters: dict[str, dict[str, Any]] = {}
        for v, day, method in self.store.rows(
            _RANKED_SQL + "SELECT v, day, voting_method FROM ranked ORDER BY v, rn"
        ):
            entry = voters.setdefault(v, {"days": [], "methods": set(), "removed": []})
            entry["days"].append(DATE_OF[day])
            entry["methods"].add(method)
        for v, ev_date in self.store.rows(
            f"SELECT {_LIVE_KEY}, ev_date FROM early_vote_day_change WHERE change_kind = 'removed'"
        ):
            voters.setdefault(v, {"days": [], "methods": set(), "removed": []})["removed"].append(
                ev_date
            )
        return {v: e for v, e in voters.items() if len(e["days"]) > 1 or e["removed"]}

    @precondition(lambda self: self.built and not self.stale)
    @invariant()
    def raw_minus_excess_is_distinct_voters(self) -> None:
        meta = dict(self.store.rows("SELECT name, value FROM early_vote_repeat_meta"))
        ((raw, distinct),) = self.store.rows(
            f"SELECT COUNT(*), COUNT(DISTINCT {_LIVE_KEY}) FROM early_vote_roster_row "
            "WHERE removed_in IS NULL"
        )
        assert int(meta["raw_rows"]) == raw
        assert int(meta["distinct_voters"]) == distinct
        assert int(meta["raw_rows"]) - int(meta["excess_rows"]) == distinct

    @precondition(lambda self: self.built and not self.stale)
    @invariant()
    def excess_slices_equal_the_sql_oracle(self) -> None:
        stored = {
            (d, c, m): n
            for d, c, m, n in self.store.rows(
                "SELECT ev_date, county, voting_method, n FROM early_vote_repeat_excess"
            )
        }
        assert stored == self._oracle_excess()

    @precondition(lambda self: self.built and not self.stale)
    @invariant()
    def repeat_rows_equal_the_oracle(self) -> None:
        oracle = self._oracle_voters()
        stored = {
            row[0]: row
            for row in self.store.rows(
                "SELECT vuid, n_counted, n_removed, first_day, counted_days, removed_days, flags "
                "FROM early_vote_vuid_repeat"
            )
        }
        assert set(stored) == set(oracle), "a VUID that stops qualifying is deleted"
        for vuid, (_, n_counted, n_removed, first_day, counted, removed, flags) in stored.items():
            entry = oracle[vuid]
            days = entry["days"]
            counted_days = json.loads(counted)
            assert counted_days == sorted(set(counted_days)) == sorted(set(days))
            assert json.loads(removed) == sorted(set(entry["removed"]))
            assert (n_counted, n_removed) == (len(days), len(entry["removed"]))
            assert first_day == (days[0] if days else None)
            want = (
                (FLAG_SAME_DAY if len(set(days)) < len(days) else 0)
                | (FLAG_CROSS_DAY if len(set(days)) > 1 else 0)
                | (FLAG_CROSS_METHOD if len(entry["methods"]) > 1 else 0)
                | (FLAG_REMOVED if entry["removed"] else 0)
            )
            assert flags == want


RepeatBuildMachine.TestCase.settings = settings(
    max_examples=max(5, settings.default.max_examples // 2),
    stateful_step_count=min(20, settings.default.stateful_step_count),
)
TestRepeatBuild = RepeatBuildMachine.TestCase


# Backfill over a calendar of days.

BACKFILL_DAYS = tuple(date(2026, 10, 14) + timedelta(days=offset) for offset in range(5))
BACKFILL_HEADER = "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT"
# A day's content: the synthetic voters it lists, or None for a day with no rows. Byte-level
# variants (line ending, BOM, a blank or whitespace-only body) must not change what is stored.
EMPTY_BODIES = (b"", b"  \n", b"\r\n", (BACKFILL_HEADER + "\n").encode())
voter_sets = st.one_of(st.none(), st.frozensets(st.integers(1, 6), min_size=1))


@dataclass(frozen=True)
class DayPayload:
    voters: frozenset[int] | None
    crlf: bool
    bom: bool
    empty_body: int


payloads = st.builds(
    DayPayload, voter_sets, st.booleans(), st.booleans(), st.integers(0, len(EMPTY_BODIES) - 1)
)


def payload_bytes(payload: DayPayload) -> bytes:
    if payload.voters is None:
        return EMPTY_BODIES[payload.empty_body]
    end = "\r\n" if payload.crlf else "\n"
    lines = [BACKFILL_HEADER] + [
        f"TEST,SYNTHETIC VOTER {n},{n:010d},IN-PERSON,1" for n in sorted(payload.voters)
    ]
    return (b"\xef\xbb\xbf" if payload.bom else b"") + (end.join(lines) + end).encode()


class _BackfillDayStore:
    """The day store the backfill writes through, keeping the last digest written per day."""

    def __init__(self) -> None:
        self.day_hashes: dict[str, str] = {}
        self.writes: list[str] = []

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        return self.day_hashes.get(ev_date_iso or "")

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
        update_pointer: bool = False,
    ) -> None:
        assert ev_date_iso
        self.writes.append(ev_date_iso)
        self.day_hashes[ev_date_iso] = digest

    def bump_version(self, election_id: str, digest: str) -> str:
        return "1"

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        return "1"


class BackfillMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.store = _BackfillDayStore()
        self.portal: dict[date, DayPayload] = {}
        self.stored: dict[str, frozenset[int]] = {}
        self.digests: dict[frozenset[int], str] = {}
        self.manifests: dict[frozenset[tuple[str, frozenset[int]]], str] = {}

    @rule(day=st.sampled_from(BACKFILL_DAYS), payload=payloads)
    def portal_publishes(self, day: date, payload: DayPayload) -> None:
        self.portal[day] = payload

    @precondition(lambda self: bool(self.portal))
    @rule(today=st.sampled_from(BACKFILL_DAYS))
    def backfill(self, today: date) -> None:
        calendar = tuple(sorted(self.portal))
        pulls: Counter[date] = Counter()

        def pull(_election_id: str, ev_day: date, **_: object) -> httpx.Response:
            pulls[ev_day] += 1
            upload = base64.b64encode(payload_bytes(self.portal[ev_day])).decode("ascii")
            return httpx.Response(200, json={"upload": upload})

        with patch(
            "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
            side_effect=pull,
        ):
            plan = plan_backfill_load_dates(ELECTION, calendar, today=today)
            result = run_backfill(
                ELECTION, self.store, plan, payload_kind="statewide_roster", defer_kv_bump=True
            )

        past = [day for day in calendar if day <= today]
        assert pulls == Counter(dict.fromkeys(past, 1)), "each past day once, no future day"
        loaded = {d.isoformat(): self.portal[d].voters for d in past if self.portal[d].voters}
        empty = [d.isoformat() for d in past if self.portal[d].voters is None]
        wrote = [iso for iso, voters in loaded.items() if self.stored.get(iso) != voters]
        assert list(result.skipped_empty_dates) == empty
        assert result.days_wrote == len(wrote)
        assert result.days_unchanged == len(loaded) - len(wrote)
        for day_result in result.days:
            voters = loaded[day_result.ev_date_iso]
            known = self.digests.setdefault(voters, day_result.sha256)
            assert day_result.sha256 == known, "a day's digest depends only on its rows"
        for voters, digest in self.digests.items():
            assert [v for v, d in self.digests.items() if d == digest] == [voters]
        state = frozenset(loaded.items())
        if not loaded:
            assert result.manifest_sha256 == ""
        else:
            known = self.manifests.setdefault(state, result.manifest_sha256)
            assert result.manifest_sha256 == known
            assert [s for s, m in self.manifests.items() if m == known] == [state]
        self.stored.update(loaded)

    @invariant()
    def stored_hashes_follow_the_model(self) -> None:
        assert set(self.store.day_hashes) == set(self.stored)
        for iso, voters in self.stored.items():
            assert self.store.day_hashes[iso] == self.digests[voters]


BackfillMachine.TestCase.settings = settings(
    max_examples=max(5, settings.default.max_examples // 2),
    stateful_step_count=min(20, settings.default.stateful_step_count),
)
TestBackfill = BackfillMachine.TestCase

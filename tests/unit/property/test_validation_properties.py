"""Properties of record validation, the reject paths, canonicalization and precinct comparison.

Covered: VUIDs leave every parser as 10-digit strings with leading zeros kept; a row is either a
model or a reject whose reason names fields and error types only (checked with sentinel cells);
every cell of a roster line lands in a field, in ``extra`` or in the reject's ``raw``; row keys
and parsed rows do not depend on BOM, CRLF or quoting; canonicalization is idempotent and keeps
every row; voter dates that cannot be used are flagged, never fatal; models round-trip through
dict and JSON; the roster CSV writer and reader round-trip; precinct comparison is reflexive,
symmetric and ignores zero padding. All data is synthetic.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import string
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.unit.property.strategies_records import (
    GOOD,
    SENTINEL,
    CsvStyle,
    RosterHeader,
    RosterLine,
    cell_text,
    county_name,
    csv_styles,
    dated_entries,
    fake_name,
    generic_rows,
    padded_vuid,
    padding,
    roster_files,
    serialize,
    voterfile_rows,
    vuid_digits,
)
from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.early_vote_roster import canonicalize_roster_csv
from texas_turnout_scraper.early_vote_roster_rows import RosterSchemaError
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.geo_models import GeoArea, GeoPrecinct, GeoPrecinctAlias
from texas_turnout_scraper.geo_tables import ALIAS_RULES, AREA_KINDS
from texas_turnout_scraper.models import VoterRecord
from texas_turnout_scraper.repeat_build import repeat_record
from texas_turnout_scraper.repeat_models import RepeatExcess, VuidRepeat
from texas_turnout_scraper.repeat_tables import FLAGS_ALL
from texas_turnout_scraper.roster import _parse_county_csv
from texas_turnout_scraper.roster_models import (
    RosterRow,
    iter_row_keys,
    parse_roster_batches,
)
from texas_turnout_scraper.voter_models import Voter, VoterReject, parse_voter, voter_row
from texas_turnout_scraper.voter_tables import (
    DOB_MIN_YEAR,
    EDR_MIN_YEAR,
    FLAG_DOB_INVALID,
    FLAG_EDR_INVALID,
    GENERAL_CODES,
    HISTORY_COLUMNS,
    PRIMARIES_AND_RUNOFFS,
    PRIMARY_CODES,
    SEXES,
    VOTER_COLUMNS,
    VOTERFILE_COLUMNS,
)
from texas_turnout_scraper.voterfile import normalize_precinct, precincts_match
from texas_turnout_scraper.writer import read_roster_csv, write_roster_csv

REASON = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)*:[a-z_]+(,[a-z0-9_]+(\.[a-z0-9_]+)*:[a-z_]+)*")
VUID = re.compile(r"[0-9]{10}")


def _probe_csv_cr_quoting() -> bool:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerow(["a\rb"])
    return not out.getvalue().startswith('"')


CSV_LEAVES_CR_UNQUOTED = _probe_csv_cr_quoting()


def reason_fields(reason: str) -> set[str]:
    return {item.split(":")[0].split(".")[0] for item in reason.split(",")}


def _parse_all(decoded: bytes, batch_rows: int = 4000) -> tuple[list[RosterRow], list]:
    rows, rejects = [], []
    for batch in parse_roster_batches(decoded, day=1, added_in=1, batch_rows=batch_rows):
        rows += batch.rows
        rejects += batch.rejects
    return rows, rejects


def _row_view(row: RosterRow) -> tuple:
    return (
        row.county,
        row.voter_name,
        row.id_voter,
        row.voting_method,
        row.precinct,
        tuple(sorted(row.extra.items())),
    )


def _expected_view(header: RosterHeader, line: RosterLine) -> tuple:
    cell = dict(zip(header.canonical, line.cells))
    extra = {header.cells[i]: line.cells[i] for i, name in enumerate(header.canonical) if not name}
    return (
        cell["COUNTY"],
        cell["VOTER_NAME"],
        line.expected_vuid,
        cell["VOTING_METHOD"].strip().upper(),
        cell.get("PRECINCT", ""),
        tuple(sorted(extra.items())),
    )


_DEFECT_FIELDS = {
    "bad_vuid": {"id_voter"},
    "blank_county": {"county"},
    "blank_name": {"voter_name"},
    "bad_method": {"voting_method"},
}


def _file(header: RosterHeader, lines: list[RosterLine], style: CsvStyle) -> bytes:
    return serialize([header.cells, *(line.cells for line in lines)], style)


_LF = CsvStyle("\n", False, False)


@given(roster_files(), csv_styles, st.integers(1, 5))
def test_roster_lines_become_exactly_one_row_or_reject(
    file: tuple[RosterHeader, list[RosterLine]], style: CsvStyle, batch_rows: int
) -> None:
    header, lines = file
    decoded = _file(header, lines, style)
    batches = list(parse_roster_batches(decoded, day=7, added_in=3, batch_rows=batch_rows))
    assert all(len(b.rows) + len(b.rejects) <= batch_rows for b in batches)
    rows = [r for b in batches for r in b.rows]
    rejects = [r for b in batches for r in b.rejects]

    good = [line for line in lines if line.defect == GOOD]
    assert Counter(_row_view(r) for r in rows) == Counter(_expected_view(header, g) for g in good)
    assert len(rejects) == len(lines) - len(good)
    for row in rows:
        assert isinstance(row.id_voter, str) and VUID.fullmatch(row.id_voter)
        assert (row.day, row.added_in) == (7, 3)

    expected_raw = Counter()
    for line in lines:
        if line.defect == GOOD:
            continue
        raw = dict(zip(header.cells, line.cells))
        if len(line.cells) > len(header.cells):
            raw["_overflow"] = list(line.cells[len(header.cells) :])
        expected_raw[repr(sorted(raw.items()))] += 1
    assert Counter(repr(sorted(r.raw.items())) for r in rejects) == expected_raw

    for reject in rejects:
        assert SENTINEL not in reject.reason
        assert reject.reason == "column_count" or REASON.fullmatch(reject.reason)


@given(roster_files())
def test_reject_reasons_name_exactly_the_bad_fields(
    file: tuple[RosterHeader, list[RosterLine]],
) -> None:
    header, lines = file
    for line in lines:
        rows, rejects = _parse_all(_file(header, [line], _LF))
        if line.defect == GOOD:
            assert len(rows) == 1 and not rejects
        elif line.defect in ("short", "long"):
            assert [r.reason for r in rejects] == ["column_count"]
        else:
            assert not rows
            assert reason_fields(rejects[0].reason) == _DEFECT_FIELDS[line.defect]


@given(roster_files(), csv_styles, csv_styles)
def test_row_keys_and_rows_ignore_bom_line_endings_and_quoting(
    file: tuple[RosterHeader, list[RosterLine]], first: CsvStyle, second: CsvStyle
) -> None:
    header, lines = file
    a, b = _file(header, lines, first), _file(header, lines, second)
    canonical, _ = canonicalize_roster_csv(a)
    assert list(iter_row_keys(a)) == list(iter_row_keys(b)) == list(iter_row_keys(canonical))
    views = [[_row_view(r) for r in _parse_all(data)[0]] for data in (a, b, canonical)]
    assert views[0] == views[1] == views[2]


@given(roster_files())
def test_identical_lines_get_distinct_occurrence_numbers(
    file: tuple[RosterHeader, list[RosterLine]],
) -> None:
    header, lines = file
    keys = list(iter_row_keys(_file(header, lines, _LF)))
    assert len(keys) == len(set(keys)) == len(lines)
    by_cells: dict[tuple[str, ...], list[int]] = {}
    for line, (_, occ) in zip(lines, keys):
        by_cells.setdefault(line.cells, []).append(occ)
    assert all(occs == list(range(len(occs))) for occs in by_cells.values())


@given(roster_files(), st.data())
def test_wanted_selects_exactly_those_keys(
    file: tuple[RosterHeader, list[RosterLine]], data: st.DataObject
) -> None:
    header, lines = file
    decoded = _file(header, lines, _LF)
    keys = list(iter_row_keys(decoded))
    wanted = set(data.draw(st.lists(st.sampled_from(keys), unique=True))) if keys else set()
    got = [
        (item.row_hash, item.occ)
        for batch in parse_roster_batches(decoded, day=1, added_in=1, wanted=wanted)
        for item in (*batch.rows, *batch.rejects)
    ]
    assert sorted(got) == sorted(wanted)


@given(roster_files())
def test_roster_rows_round_trip_through_dict_and_json(
    file: tuple[RosterHeader, list[RosterLine]],
) -> None:
    header, lines = file
    for row in _parse_all(_file(header, lines, _LF))[0]:
        dumped = row.model_dump()
        assert RosterRow.model_validate(dumped).model_dump() == dumped
        assert RosterRow.model_validate_json(row.model_dump_json()).model_dump() == dumped


_HEADER_DEFECTS = st.sampled_from(["missing", "duplicate"])


@given(roster_files(max_lines=3), _HEADER_DEFECTS, st.data())
def test_bad_header_raises_before_any_batch_naming_headers_only(
    file: tuple[RosterHeader, list[RosterLine]], defect: str, data: st.DataObject
) -> None:
    header, lines = file
    cells = list(header.cells)
    if defect == "missing":
        position = data.draw(
            st.sampled_from([i for i, n in enumerate(header.canonical) if n and n != "PRECINCT"])
        )
        del cells[position]
        body = [line.cells[:position] + line.cells[position + 1 :] for line in lines]
    else:
        position = data.draw(st.sampled_from(range(len(cells))))
        cells.append(" " + cells[position].lower() + " ")
        body = [(*line.cells, SENTINEL) for line in lines]
    decoded = serialize([cells, *body], _LF)
    with pytest.raises(RosterSchemaError) as raised:
        parse_roster_batches(decoded, day=1, added_in=1)
    assert SENTINEL not in str(raised.value)


@given(generic_rows, csv_styles, csv_styles)
@example((1, [[""], [""]]), CsvStyle("\r\n", True, False), _LF)
@example((2, [["a", 'say "hi", ok'], ["x\ny", ""]]), CsvStyle("\r\n", True, True), _LF)
@example((1, []), CsvStyle("\n", True, False), _LF)
def test_canonicalization_keeps_rows_and_is_idempotent(
    shaped: tuple[int, list[list[str]]], first: CsvStyle, second: CsvStyle
) -> None:
    _, rows = shaped
    canonical, summary = canonicalize_roster_csv(serialize(rows, first))
    other, _ = canonicalize_roster_csv(serialize(rows, second))
    assert canonical == other
    assert canonicalize_roster_csv(canonical)[0] == canonical
    assert not canonical.startswith(b"\xef\xbb\xbf") and b"\r" not in canonical
    assert list(csv.reader(io.StringIO(canonical.decode("utf-8")))) == rows
    assert summary.est_rows == max(len(rows) - 1, 0)
    assert summary.columns == (tuple(rows[0]) if rows else ())


@given(roster_files())
def test_canonical_method_counts_match_the_lines(
    file: tuple[RosterHeader, list[RosterLine]],
) -> None:
    header, lines = file
    _, summary = canonicalize_roster_csv(_file(header, lines, _LF))
    at = header.position("VOTING_METHOD")
    expected = Counter(
        line.cells[at].strip() for line in lines if at < len(line.cells) and line.cells[at].strip()
    )
    assert dict(summary.voting_method_counts) == dict(expected)
    assert summary.est_rows == len(lines)


@pytest.mark.xfail(
    CSV_LEAVES_CR_UNQUOTED,
    strict=True,
    raises=csv.Error,
    reason=(
        "Python 3.10 csv.writer leaves a lone CR unquoted: canonicalize_roster_csv of "
        "b'COUNTY,VOTER_NAME\\nA,\"x\\ry\"\\n' emits x\\ry bare and re-reading it raises csv.Error"
    ),
)
@given(st.lists(cell_text, min_size=1, max_size=3), st.integers(0, 2))
@example(["x", "y"], 0)
def test_canonicalization_survives_a_carriage_return_inside_a_cell(
    cells: list[str], at: int
) -> None:
    at = min(at, len(cells) - 1)
    cells[at] = cells[at] + "\r" + "z"
    rows = [[f"H{i}" for i in range(len(cells))], cells]
    canonical, _ = canonicalize_roster_csv(serialize(rows, _LF))
    assert list(csv.reader(io.StringIO(canonical.decode("utf-8")))) == rows
    assert canonicalize_roster_csv(canonical)[0] == canonical


def _clean(value: str | None) -> str | None:
    text = None if value is None else value.strip()
    return text or None


def _expected_date(cell: str | None, min_year: int, today: date) -> tuple[str | None, bool]:
    text = _clean(cell)
    if text is None:
        return None, False
    if not re.fullmatch(r"[0-9]{8}", text):
        return None, True
    try:
        parsed = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None, True
    if parsed.year < min_year or parsed > today:
        return None, True
    return parsed.isoformat(), False


def _expected_bad_fields(raw: dict[str, str | None]) -> set[str]:
    bad = set()
    vuid = _clean(raw.get("VUID"))
    if vuid is None or not re.fullmatch(r"[0-9]{1,10}", vuid):
        bad.add("vuid")
    if _clean(raw.get("SEX")) not in SEXES:
        bad.add("sex")
    for column in HISTORY_COLUMNS:
        code = _clean(raw.get(column))
        allowed = GENERAL_CODES if column.startswith("GEN") else PRIMARY_CODES
        if code is not None and code not in allowed:
            bad.add(column.lower())
    return bad


def _voter_raw(**cells: str | None) -> dict[str, str | None]:
    raw: dict[str, str | None] = {column: None for column in VOTERFILE_COLUMNS}
    raw.update({"VUID": "1", "SEX": "F"})
    raw.update(cells)
    return raw


_NO_DATED = {"GEN25": None, "PRI26": None, "RO26": None}
_TODAY = date(2026, 10, 25)


@given(
    voterfile_rows(),
    dated_entries(),
    st.dates(min_value=date(1990, 1, 1), max_value=date(2100, 12, 31)),
)
@example(_voter_raw(DOB="20240229"), _NO_DATED, date(2024, 2, 28))
@example(_voter_raw(DOB="20240229"), _NO_DATED, date(2024, 2, 29))
@example(_voter_raw(DOB="19000229", EDR="18991231"), _NO_DATED, _TODAY)
@example(_voter_raw(DOB="18800101", EDR="19000101"), _NO_DATED, _TODAY)
@example(_voter_raw(VUID="0000000001"), _NO_DATED, _TODAY)
@example(_voter_raw(VUID=" 12345 ", GEN24="VE", PRI26="RA", RO26="D"), _NO_DATED, _TODAY)
@example(_voter_raw(VUID="12345678901"), _NO_DATED, _TODAY)
@example(_voter_raw(VUID=SENTINEL, SEX=SENTINEL, GEN24=SENTINEL), _NO_DATED, _TODAY)
@example(_voter_raw(PRI24="V", GEN22="R"), _NO_DATED, _TODAY)
def test_parse_voter_matches_an_independent_oracle(
    raw: dict[str, str | None], dated: dict[str, tuple[int, str] | None], today: date
) -> None:
    parsed = parse_voter(raw, dated, today=today)
    bad = _expected_bad_fields(raw)
    if bad:
        assert isinstance(parsed, VoterReject)
        assert REASON.fullmatch(parsed.reason) and SENTINEL not in parsed.reason
        assert reason_fields(parsed.reason) == bad
        assert parsed.raw == {column: raw.get(column) for column in VOTERFILE_COLUMNS}
        cleaned = _clean(raw.get("VUID")) or ""
        digits = cleaned.isascii() and cleaned.isdigit()
        assert parsed.vuid == (cleaned.zfill(10) if digits else cleaned)
        return

    assert isinstance(parsed, Voter)
    assert VUID.fullmatch(parsed.vuid) and parsed.vuid == (_clean(raw["VUID"]) or "").zfill(10)
    dob, dob_bad = _expected_date(raw["DOB"], DOB_MIN_YEAR, today)
    edr, edr_bad = _expected_date(raw["EDR"], EDR_MIN_YEAR, today)
    assert (parsed.dob, parsed.edr) == (dob, edr)
    assert parsed.flags == (FLAG_DOB_INVALID if dob_bad else 0) | (
        FLAG_EDR_INVALID if edr_bad else 0
    )
    history = {c: _clean(raw.get(c)) for c in HISTORY_COLUMNS}
    assert {c: getattr(parsed, c.lower()) for c in HISTORY_COLUMNS} == history
    voted = [code for code in history.values() if code is not None]
    early = sum(len(code) > 1 and code.endswith("E") for code in voted)
    absentee = sum(len(code) > 1 and code.endswith("A") for code in voted)
    assert (parsed.n_voted, parsed.n_early, parsed.n_absentee, parsed.n_election_day) == (
        len(voted),
        early,
        absentee,
        len(voted) - early - absentee,
    )
    parties = [history[c][0] for c in PRIMARIES_AND_RUNOFFS if history[c] is not None]
    assert (parsed.n_primary_r, parsed.n_primary_d) == (parties.count("R"), parties.count("D"))
    for key, day_field in (("GEN25", "gen25_day"), ("PRI26", "pri26_day"), ("RO26", "ro26_day")):
        entry = dated.get(key)
        assert getattr(parsed, day_field) == (None if entry is None else entry[0])
    gen25 = dated.get("GEN25")
    assert parsed.gen25_mode == (None if gen25 is None else gen25[1])

    assert Voter.model_validate(parsed.model_dump()) == parsed
    assert Voter.model_validate_json(parsed.model_dump_json()) == parsed
    assert dict(zip(VOTER_COLUMNS, voter_row(parsed))) == parsed.model_dump()

    stripped = {c: _clean(raw.get(c)) for c in VOTERFILE_COLUMNS}
    assert parse_voter(stripped, dated, today=today) == parsed


@settings(max_examples=20)
@given(st.lists(voterfile_rows(), max_size=4), st.dates(min_value=date(1990, 1, 1)))
def test_bad_dates_never_abort_a_batch(rows: list[dict[str, str | None]], today: date) -> None:
    parsed = [parse_voter(raw, _NO_DATED, today=today) for raw in rows]
    assert len(parsed) == len(rows)
    voters = [p for p in parsed if isinstance(p, Voter)]
    flagged_dob = sum(
        _expected_date(r["DOB"], DOB_MIN_YEAR, today)[1]
        for r in rows
        if not _expected_bad_fields(r)
    )
    assert sum(bool(v.flags & FLAG_DOB_INVALID) for v in voters) == flagged_dob
    assert len(voters) == sum(not _expected_bad_fields(r) for r in rows)


_DAYS = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)).map(date.isoformat)
_METHODS = st.sampled_from(["IN-PERSON", "MAIL-IN"])


@st.composite
def vuid_repeats(draw: st.DrawFn) -> dict[str, object]:
    return {
        "vuid": draw(st.text(string.digits, min_size=10, max_size=10)),
        "n_counted": draw(st.integers(0, 2**31)),
        "n_removed": draw(st.integers(0, 2**31)),
        "first_day": draw(st.none() | _DAYS),
        "counted_days": sorted(set(draw(st.lists(_DAYS, max_size=4)))),
        "appearances": [
            {"day": d, "method": m, "county": c}
            for d, m, c in draw(st.lists(st.tuples(_DAYS, _METHODS, county_name), max_size=4))
        ],
        "removed_days": sorted(set(draw(st.lists(_DAYS, max_size=3)))),
        "flags": draw(st.integers(0, FLAGS_ALL)),
        "src_hash": draw(st.integers(-(2**63), 2**63 - 1)),
    }


_REPEAT_BREAKS = {
    "vuid": ["123456789", "12345678901", SENTINEL[:10], "\uff11" * 10, ""],
    "n_counted": [-1, -(2**63)],
    "n_removed": [-1],
    "first_day": [SENTINEL, "2023-02-29", "2024-13-01"],
    "counted_days": [["2024-01-02", "2024-01-01"], ["2024-01-01", "2024-01-01"], [SENTINEL]],
    "removed_days": [["2024-03-02", "2024-03-01"], ["2024-02-30"]],
    "appearances": [
        [{"method": "MAIL-IN", "day": "2024-01-01", "county": "X"}],
        [{"day": "2024-01-01", "method": SENTINEL, "county": "X"}],
        [{"day": SENTINEL, "method": "MAIL-IN", "county": "X"}],
        [{"day": "2024-01-01", "method": "MAIL-IN"}],
    ],
    "flags": [FLAGS_ALL + 1, 16, -1],
}


@given(vuid_repeats(), st.sampled_from(sorted(_REPEAT_BREAKS)), st.data())
def test_vuid_repeat_accepts_valid_rows_and_rejects_by_field(
    fields: dict[str, object], broken: str, data: st.DataObject
) -> None:
    model = VuidRepeat.model_validate(fields)
    assert model.model_dump() == fields
    assert VuidRepeat.model_validate_json(model.model_dump_json()) == model

    bad = {**fields, broken: data.draw(st.sampled_from(_REPEAT_BREAKS[broken]))}
    with pytest.raises(ValidationError) as raised:
        VuidRepeat.model_validate(bad)
    assert {error["loc"][0] for error in raised.value.errors()} == {broken}


@given(
    st.text(string.digits, min_size=10, max_size=10),
    st.lists(st.tuples(_DAYS, county_name, _METHODS), max_size=4),
    st.lists(_DAYS, max_size=3),
)
def test_repeat_record_rows_validate_and_round_trip(
    vuid: str, appearances: list[tuple[str, str, str]], removals: list[str]
) -> None:
    ordered = sorted(appearances)
    record = repeat_record(vuid, ordered, removals)
    assert record.vuid == vuid
    assert record.counted_days == sorted({day for day, _, _ in ordered})
    assert VuidRepeat.model_validate(record.model_dump()) == record


@given(
    st.sampled_from([SENTINEL, SENTINEL[:10], "123"]),
    st.sampled_from(["vuid", "method", "day"]),
)
def test_repeat_record_failures_name_fields_not_values(value: str, where: str) -> None:
    vuid = value if where == "vuid" else "0000000001"
    method = value if where == "method" else "IN-PERSON"
    day = value if where == "day" else "2024-10-21"
    with pytest.raises(EarlyVoteSyncError) as raised:
        repeat_record(vuid, [(day, "FAKECOUNTY", method)], [])
    message = str(raised.value)
    assert value not in message
    assert REASON.fullmatch(message.removeprefix("repeat row failed validation: "))


@given(_DAYS, county_name, st.text(max_size=12), st.integers(-3, 2**31))
@example("2024-10-21", "X", "IN-PERSON", 1)
@example("2024-10-21", "X", "MAIL-IN", 0)
def test_repeat_excess_validates_method_and_count(
    ev_date: str, county: str, method: str, n: int
) -> None:
    row = {"ev_date": ev_date, "county": county, "voting_method": method, "n": n}
    expected_bad = {"voting_method"} if method not in ("IN-PERSON", "MAIL-IN") else set()
    expected_bad |= {"n"} if n < 1 else set()
    if not expected_bad:
        model = RepeatExcess.model_validate(row)
        assert model.model_dump() == row
        return
    with pytest.raises(ValidationError) as raised:
        RepeatExcess.model_validate(row)
    assert {error["loc"][0] for error in raised.value.errors()} == expected_bad


@given(
    st.sampled_from(sorted(AREA_KINDS)) | st.text(max_size=6),
    st.text(max_size=5) | st.text(string.digits, min_size=3, max_size=3),
    st.sampled_from(list(ALIAS_RULES)) | st.text(max_size=8),
)
@example("county", "001", "exact")
@example("County", "01", "EXACT")
@example("precinct", "\u0661\u0662\u0663", "zfill4")
def test_geo_models_accept_exactly_their_vocabularies(kind: str, fips: str, rule: str) -> None:
    area = {"kind": kind, "code": "C", "name": "N", "lat": 30.0, "lon": -97.0}
    precinct = {"pctkey": "P", "county_fips": fips, "prec": "1"}
    alias = {"county": "C", "precinct": "1", "pctkey": None, "rule": rule}
    cases = [
        (GeoArea, area, "kind", kind in AREA_KINDS),
        (GeoPrecinct, precinct, "county_fips", bool(re.fullmatch(r"[0-9]{3}", fips))),
        (GeoPrecinctAlias, alias, "rule", rule in ALIAS_RULES),
    ]
    for model, row, field, valid in cases:
        if valid:
            assert model.model_validate(row).model_dump()[field] == row[field]
        else:
            with pytest.raises(ValidationError) as raised:
                model.model_validate(row)
            assert {error["loc"][0] for error in raised.value.errors()} == {field}


@st.composite
def voter_records(draw: st.DrawFn) -> VoterRecord:
    return VoterRecord(
        id_voter=draw(vuid_digits).zfill(10),
        voting_method=draw(st.sampled_from(list(VoteMethod))),
        precinct=draw(cell_text).strip(),
        county=draw(county_name),
        election_id=draw(st.text(string.digits, min_size=1, max_size=6)),
        report_date=draw(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))),
        voter_name=(SENTINEL + draw(fake_name)).strip(),
        duplicate_flag=draw(st.booleans()),
        duplicate_type=draw(cell_text),
        also_found_on=draw(cell_text),
    )


@given(st.lists(voter_records(), max_size=6))
def test_roster_csv_write_then_read_returns_the_same_records(records: list[VoterRecord]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_roster_csv(records, Path(tmp) / "roster.csv")
        assert read_roster_csv(path) == records


@given(voter_records())
def test_voter_record_round_trips_and_never_coerces_ids(record: VoterRecord) -> None:
    assert VoterRecord.model_validate(record.model_dump()) == record
    again = VoterRecord.model_validate_json(record.model_dump_json())
    assert again == record and isinstance(again.id_voter, str)
    with pytest.raises(ValidationError):
        VoterRecord.model_validate({**record.model_dump(), "id_voter": int(record.id_voter)})


@given(padded_vuid(), st.sampled_from(["IN-PERSON", "MAIL-IN", "mail", ""]))
@example((" 0000000001 ", "0000000001"), "IN-PERSON")
@example(("1", "0000000001"), "MAIL-IN")
def test_voter_record_from_csv_row_pads_vuids(pair: tuple[str, str], method: str) -> None:
    cell, expected = pair
    record = VoterRecord.from_csv_row(
        {"ID_VOTER": cell, "VOTING_METHOD": method},
        county="X",
        election_id="1",
        report_date=date(2024, 10, 21),
    )
    assert record.id_voter == expected


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "VoterRecord.from_csv_row keeps ID_VOTER='12345678901' as an 11-character id_voter "
        "(and 'A1' as '00000000A1'); the model documents id_voter as always 10 digits"
    ),
)
@given(st.text(string.digits, min_size=11, max_size=12) | st.sampled_from(["A1", "1-2"]))
@example("12345678901")
def test_voter_record_rejects_ids_that_are_not_vuids(cell: str) -> None:
    try:
        record = VoterRecord.from_csv_row(
            {"ID_VOTER": cell, "VOTING_METHOD": "IN-PERSON"},
            county="X",
            election_id="1",
            report_date=date(2024, 10, 21),
        )
    except ValidationError:
        return
    assert VUID.fullmatch(record.id_voter)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(record.getMessage())


_COUNTY_HEADER = ("VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT")


@given(
    st.lists(
        st.tuples(
            fake_name.map(lambda n: SENTINEL + n),
            padded_vuid() | st.sampled_from([(" ", ""), ("", "")]),
            st.sampled_from(["IN-PERSON", "MAIL-IN"]),
            cell_text,
        ),
        max_size=8,
    ),
    csv_styles,
)
def test_county_csv_keeps_every_vuid_and_logs_no_row_content(
    lines: list[tuple[str, tuple[str, str], str, str]], style: CsvStyle
) -> None:
    rows = [(name, cell, method, precinct) for name, (cell, _), method, precinct in lines]
    text = serialize([_COUNTY_HEADER, *rows], CsvStyle(style.terminator, False, style.quote_all))
    capture = _Capture()
    logger = logging.getLogger("texas_turnout_scraper.roster")
    logger.addHandler(capture)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        roster = _parse_county_csv(text.decode("utf-8"), "7", "FAKECOUNTY", "1", date(2024, 10, 21))
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)
    expected = [want for _, (_, want), _, _ in lines if want]
    got = [] if roster is None else [r.id_voter for r in roster.records]
    assert got == expected
    assert all(isinstance(v, str) and VUID.fullmatch(v) for v in got)
    assert not any(SENTINEL in message for message in capture.text)
    assert not any(want in message for want in expected for message in capture.text)


@given(st.integers(1, 3), padded_vuid())
@example(2, ("123", "0000000123"))
def test_county_csv_short_row_does_not_crash(width: int, pair: tuple[str, str]) -> None:
    cells = [SENTINEL + "N", pair[0], "IN-PERSON"][:width]
    text = serialize([_COUNTY_HEADER, cells], _LF).decode("utf-8")
    roster = _parse_county_csv(text, "7", "FAKECOUNTY", "1", date(2024, 10, 21))
    assert roster is None or all(VUID.fullmatch(r.id_voter) for r in roster.records)


precinct_text = st.text(
    st.sampled_from(string.digits + "0000AbcZ -.\u00e9\u0661") | st.characters(codec="utf-8"),
    max_size=7,
)


@given(precinct_text, precinct_text)
@example("", "  ")
@example("510", "0510")
@example("ß", "SS")
def test_precinct_match_is_reflexive_and_symmetric(a: str, b: str) -> None:
    assert precincts_match(a, a)
    assert precincts_match(a, b) == precincts_match(b, a)
    assert normalize_precinct(normalize_precinct(a)) == normalize_precinct(a)


@given(st.text(string.digits, min_size=1, max_size=4), st.integers(0, 4), padding, padding)
def test_precinct_match_ignores_zero_padding_and_whitespace(
    digits: str, zeros: int, left: str, right: str
) -> None:
    assert precincts_match(left + "0" * zeros + digits + right, digits)
    assert precincts_match(digits.zfill(4), digits.lstrip("0") or "0")


@given(
    st.text(string.digits, min_size=1, max_size=7), st.text(string.digits, min_size=1, max_size=7)
)
@example("10510", "0510")
@example("0", "0000")
@example("10000", "0")
def test_digit_precincts_compare_by_their_last_four_digits(a: str, b: str) -> None:
    assert precincts_match(a, b) == (int(a) % 10_000 == int(b) % 10_000)

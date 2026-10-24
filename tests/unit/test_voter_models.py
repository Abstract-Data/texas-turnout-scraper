"""Voter rows: validation into rows or rejects, date flags, DDL drift and import hygiene."""

from __future__ import annotations

import itertools
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES
from texas_turnout_scraper.voter_models import (
    AgeTier,
    ElectionCalendarRow,
    Voter,
    VoterMeta,
    VoterReject,
    VoterRow,
    parse_voter,
    voter_row,
)
from texas_turnout_scraper.voter_tables import (
    AGE_TIER_TABLE_SQL,
    AGE_TIERS,
    CALENDAR_TABLE_SQL,
    ELECTION_CALENDAR,
    FLAG_DOB_INVALID,
    FLAG_EDR_INVALID,
    HISTORY_COLUMNS,
    META_TABLE_SQL,
    REJECT_TABLE_SQL,
    VOTER_COLUMNS,
    VOTER_SCHEMA_CREATES,
    VOTER_TABLE_SQL,
    VOTERFILE_COLUMNS,
)

BLANK: str | None = None

TODAY = date(2026, 10, 24)


def _raw(**cells: str | None) -> dict[str, str | None]:
    row: dict[str, str | None] = dict.fromkeys(VOTERFILE_COLUMNS, BLANK)
    row.update({"VUID": "1000000001", "SEX": "F", "DOB": "19800215", "EDR": "19980301"})
    row.update(cells)
    return row


def test_a_valid_row_becomes_a_voter_with_iso_dates_and_lower_case_history() -> None:
    voter = parse_voter(_raw(GEN24="VE", PRI26="RA", RO18=""), {}, today=TODAY)
    assert isinstance(voter, Voter)
    assert (voter.vuid, voter.sex, voter.dob, voter.edr, voter.flags) == (
        "1000000001",
        "F",
        "1980-02-15",
        "1998-03-01",
        0,
    )
    assert (voter.gen24, voter.pri26, voter.ro18, voter.gen22) == ("VE", "RA", None, None)
    assert (voter.n_voted, voter.n_early, voter.n_absentee) == (2, 1, 1)
    assert voter.score_version == 1


def test_a_short_vuid_is_zero_padded() -> None:
    voter = parse_voter(_raw(VUID="12345"), {}, today=TODAY)
    assert isinstance(voter, Voter)
    assert voter.vuid == "0000012345"


def test_dated_history_fills_the_day_columns() -> None:
    voter = parse_voter(
        _raw(PRI26="DE", RO26="DA"),
        {"GEN25": (3, "A"), "PRI26": (2, "E"), "RO26": (5, "A")},
        today=TODAY,
    )
    assert isinstance(voter, Voter)
    assert (voter.gen25_day, voter.gen25_mode, voter.pri26_day, voter.ro26_day) == (3, "A", 2, 5)
    assert voter.earliness_n == 3


@pytest.mark.parametrize(
    ("cells", "field"),
    [
        ({"GEN24": "X"}, "gen24"),
        ({"GEN22": "D"}, "gen22"),
        ({"PRI24": "V"}, "pri24"),
        ({"RO20": "DX"}, "ro20"),
        ({"SEX": "Q"}, "sex"),
        ({"SEX": None}, "sex"),
        ({"VUID": "12AB"}, "vuid"),
    ],
)
def test_an_invalid_code_or_sex_is_rejected_with_a_value_free_reason_and_the_whole_row(
    cells: dict[str, str | None], field: str
) -> None:
    raw = _raw(**cells)
    reject = parse_voter(raw, {}, today=TODAY)
    assert isinstance(reject, VoterReject)
    assert reject.reason.split(":")[0] == field
    assert reject.raw == raw
    assert set(reject.raw) == set(VOTERFILE_COLUMNS)
    for value in cells.values():
        if value is not None:
            assert value not in reject.reason
    assert "19800215" not in reject.reason


@pytest.mark.parametrize(
    ("dob", "edr", "expected_dob", "expected_edr", "flags"),
    [
        ("00010101", "19980301", None, "1998-03-01", FLAG_DOB_INVALID),
        ("19800215", "98640101", "1980-02-15", None, FLAG_EDR_INVALID),
        ("00010101", "98640101", None, None, FLAG_DOB_INVALID | FLAG_EDR_INVALID),
        ("20230230", "19980301", None, "1998-03-01", FLAG_DOB_INVALID),
        ("1980-02-15", "19980301", None, "1998-03-01", FLAG_DOB_INVALID),
        ("19800215", "abc", "1980-02-15", None, FLAG_EDR_INVALID),
        (None, None, None, None, 0),
        ("", " ", None, None, 0),
        ("18800101", "19000101", "1880-01-01", "1900-01-01", 0),
        ("18791231", "18991231", None, None, FLAG_DOB_INVALID | FLAG_EDR_INVALID),
        ("20261024", "20261024", "2026-10-24", "2026-10-24", 0),
        ("20261025", "20261025", None, None, FLAG_DOB_INVALID | FLAG_EDR_INVALID),
    ],
)
def test_junk_dates_become_null_with_a_flag_and_the_voter_still_loads(
    dob: str | None, edr: str | None, expected_dob: str | None, expected_edr: str | None, flags: int
) -> None:
    voter = parse_voter(_raw(DOB=dob, EDR=edr), {}, today=TODAY)
    assert isinstance(voter, Voter)
    assert (voter.dob, voter.edr, voter.flags) == (expected_dob, expected_edr, flags)


def test_src_hash_changes_with_any_input_and_with_the_dated_history() -> None:
    base = parse_voter(_raw(GEN24="V"), {}, today=TODAY)
    same = parse_voter(_raw(GEN24="V"), {}, today=TODAY)
    changed = parse_voter(_raw(GEN24="VE"), {}, today=TODAY)
    dated = parse_voter(_raw(GEN24="V"), {"GEN25": (1, "E")}, today=TODAY)
    assert isinstance(base, Voter) and isinstance(same, Voter)
    assert isinstance(changed, Voter) and isinstance(dated, Voter)
    assert base.src_hash == same.src_hash
    assert len({base.src_hash, changed.src_hash, dated.src_hash}) == 3


def test_voter_row_follows_the_upsert_column_order() -> None:
    voter = parse_voter(_raw(GEN24="VE"), {}, today=TODAY)
    assert isinstance(voter, Voter)
    row = voter_row(voter)
    assert len(row) == len(VOTER_COLUMNS)
    assert dict(zip(VOTER_COLUMNS, row, strict=True))["gen24"] == "VE"
    assert row[0] == "1000000001"


def test_the_table_constructor_skips_validation_so_ingest_uses_model_validate() -> None:
    row = VoterRow(vuid="x", sex="Q")  # ty: ignore[missing-argument]  # reason: pins the bypass
    assert row.sex == "Q"


def test_calendar_seed_matches_the_portal_verified_elections() -> None:
    assert [
        (e.key, e.election_date, e.early_days, e.first_early_date, e.is_current, e.civix_ids)
        for e in ELECTION_CALENDAR
    ] == [
        ("GEN25", "2025-11-04", 12, "2025-10-20", False, (51031,)),
        ("PRI26", "2026-03-03", 11, "2026-02-17", False, (53813, 53814)),
        ("RO26", "2026-05-26", 5, "2026-05-18", False, (58314, 58315)),
        ("GEN26", "2026-11-03", 17, "2026-10-14", True, (53815,)),
    ]


def test_age_tiers_cover_18_to_200_without_gaps_or_overlap() -> None:
    assert [t[2] for t in AGE_TIERS] == [
        "18-24",
        "25-34",
        "35-54",
        "55-64",
        "65-74",
        "75-84",
        "85+",
    ]
    assert AGE_TIERS[0][0] == 18 and AGE_TIERS[-1][1] == 200
    for (_, high, _, _), (low, _, _, _) in itertools.pairwise(AGE_TIERS):
        assert low == high + 1


def test_the_link_reads_exactly_the_21_owner_chosen_columns() -> None:
    assert len(VOTERFILE_COLUMNS) == 21
    assert len(HISTORY_COLUMNS) == 17
    for excluded in ("COUNTY", "PCT", "CDPLANC2333", "SD2022", "HD2022"):
        assert excluded not in VOTERFILE_COLUMNS


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.mark.parametrize(
    ("model", "constant"),
    [
        (VoterRow, VOTER_TABLE_SQL),
        (ElectionCalendarRow, CALENDAR_TABLE_SQL),
        (AgeTier, AGE_TIER_TABLE_SQL),
        (VoterReject, REJECT_TABLE_SQL),
        (VoterMeta, META_TABLE_SQL),
    ],
)
def test_committed_voter_ddl_matches_what_the_models_generate(model: Any, constant: str) -> None:
    generated = str(
        CreateTable(model.__table__, if_not_exists=True).compile(dialect=sqlite.dialect())
    )
    assert _normalize(constant) == _normalize(generated)


def test_voter_columns_match_the_voter_model() -> None:
    assert set(VOTER_COLUMNS) == set(VoterRow.__table__.columns.keys())  # ty: ignore[unresolved-attribute]  # reason: SQLModel sets it at class creation


def test_the_sync_schema_creates_the_voter_tables_and_view() -> None:
    for sql in VOTER_SCHEMA_CREATES:
        assert sql in _SCHEMA_CREATES


_HEAVY = "('sqlmodel', 'sqlalchemy', 'duckdb', 'shapely', 'pyproj')"


@pytest.mark.parametrize(
    "module",
    [
        "texas_turnout_scraper.early_vote_sync",
        "texas_turnout_scraper.geo_tables",
        "texas_turnout_scraper.voter_tables",
        "texas_turnout_scraper.voter_scores",
    ],
)
def test_worker_side_modules_import_no_heavy_libraries(module: str) -> None:
    code = (
        f"import sys, {module};bad = [m for m in {_HEAVY} if m in sys.modules];assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_worker_app_import_graph_loads_no_heavy_libraries() -> None:
    worker_src = Path(__file__).resolve().parents[2] / "infra" / "preview" / "worker" / "src"
    code = (
        f"import sys, ev_api.app; bad = [m for m in {_HEAVY} if m in sys.modules];"
        "assert not bad, bad"
    )
    env = {**os.environ, "PYTHONPATH": str(worker_src)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_the_link_module_imports_duckdb_only_when_it_reads_the_voterfile() -> None:
    code = "import sys, texas_turnout_scraper.voter_link;assert 'duckdb' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)

"""Roster rows as SQLModel models: validation, full-column capture, quarantine, generated DDL."""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.early_vote_roster_rows import (
    REJECT_TABLE_SQL,
    ROW_TABLE_SQL,
    VERSION_TABLE_SQL,
    RosterSchemaError,
    check_roster_header,
)
from texas_turnout_scraper.roster_models import (
    RosterReject,
    RosterRow,
    RosterVersion,
    iter_row_keys,
    parse_roster_batches,
)

NAME = "ZEBEDIAH QUINCEWORTH"
GOLDEN_HASH = 6712502547067982623

GOOD: dict[str, Any] = {
    "day": 740000,
    "row_hash": -5,
    "occ": 0,
    "added_in": 1,
    "county": "TRAVIS",
    "voter_name": NAME,
    "id_voter": " 12345 ",
    "voting_method": " mail-in ",
}


def _parse(text: str) -> tuple[list[RosterRow], list[RosterReject]]:
    rows: list[RosterRow] = []
    rejects: list[RosterReject] = []
    for batch in parse_roster_batches(text.encode(), day=1, added_in=1):
        rows.extend(batch.rows)
        rejects.extend(batch.rejects)
    return rows, rejects


def test_a_valid_row_normalizes_vuid_and_method() -> None:
    row = RosterRow.model_validate(GOOD)
    assert row.id_voter == "0000012345"
    assert row.voting_method == "MAIL-IN"
    assert row.precinct == ""
    assert row.extra == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("county", "   "),
        ("voter_name", ""),
        ("id_voter", "12A45"),
        ("id_voter", "12345678901"),
        ("id_voter", ""),
        ("voting_method", "EARLY"),
    ],
)
def test_each_rule_rejects_a_bad_value(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        RosterRow.model_validate({**GOOD, field: value})
    assert [error["loc"] for error in caught.value.errors()] == [(field,)]


def test_the_table_constructor_skips_validation_so_ingest_must_use_model_validate() -> None:
    row = RosterRow(
        day=1,
        row_hash=1,
        occ=0,
        added_in=1,
        county="TRAVIS",
        voter_name=NAME,
        id_voter="NOT-A-VUID",
        voting_method="MAIL-IN",
    )
    assert row.id_voter == "NOT-A-VUID"


def test_every_unknown_column_is_kept_in_extra_including_polling_and_empty_values() -> None:
    rows, rejects = _parse(
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT,POLL_PLACE_NAME,Vote Site,"
        "BALLOT_STYLE,NOTE\n"
        f"TRAVIS,{NAME},1,IN-PERSON,101,CITY HALL,SITE 4,BS-12,\n"
    )
    assert rejects == []
    assert rows[0].extra == {
        "POLL_PLACE_NAME": "CITY HALL",
        "Vote Site": "SITE 4",
        "BALLOT_STYLE": "BS-12",
        "NOTE": "",
    }


def test_known_headers_match_case_and_space_insensitively_and_precinct_is_optional() -> None:
    rows, _ = _parse(f" id_voter ,County,Voter_Name,VOTING_METHOD\n7,BEXAR,{NAME},MAIL-IN\n")
    assert (rows[0].county, rows[0].id_voter, rows[0].precinct) == ("BEXAR", "0000000007", "")


def test_a_bad_row_is_quarantined_whole_and_its_reason_has_no_cell_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("DEBUG"):
        rows, rejects = _parse(
            "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,POLL\n"
            f"TRAVIS,{NAME},BADVUID,EARLY,SITE\n"
            "BEXAR,OK NAME,2,MAIL-IN,X\n"
        )
    assert [row.county for row in rows] == ["BEXAR"]
    assert len(rejects) == 1
    reject = rejects[0]
    first_key = next(
        iter_row_keys(
            f"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,POLL\nTRAVIS,{NAME},BADVUID,EARLY,SITE\n".encode()
        )
    )
    assert (reject.row_hash, reject.occ, reject.added_in, reject.removed_in) == (
        *first_key,
        1,
        None,
    )
    assert reject.raw == {
        "COUNTY": "TRAVIS",
        "VOTER_NAME": NAME,
        "ID_VOTER": "BADVUID",
        "VOTING_METHOD": "EARLY",
        "POLL": "SITE",
    }
    assert reject.reason == "id_voter:value_error,voting_method:value_error"
    assert NAME not in reject.reason
    assert "BADVUID" not in reject.reason
    assert NAME not in caplog.text


def test_a_row_with_the_wrong_cell_count_is_rejected_with_its_overflow() -> None:
    _, rejects = _parse(
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\n"
        f"TRAVIS,{NAME},1,MAIL-IN,SURPLUS,MORE\n"
        "BEXAR,SHORT\n"
    )
    assert [r.reason for r in rejects] == ["column_count", "column_count"]
    assert rejects[0].raw == {
        "COUNTY": "TRAVIS",
        "VOTER_NAME": NAME,
        "ID_VOTER": "1",
        "VOTING_METHOD": "MAIL-IN",
        "_overflow": ["SURPLUS", "MORE"],
    }
    assert rejects[1].raw == {"COUNTY": "BEXAR", "VOTER_NAME": "SHORT"}


def test_blank_lines_are_skipped_and_identical_lines_count_up_their_occurrence() -> None:
    rows, rejects = _parse(
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\n"
        "A,N1,1,MAIL-IN\n"
        "\n"
        "B,N2,X,MAIL-IN\n"
        "A,N1,1,MAIL-IN\n"
        "B,N2,X,MAIL-IN\n"
    )
    assert [(row.county, row.occ) for row in rows] == [("A", 0), ("A", 1)]
    assert [reject.occ for reject in rejects] == [0, 1]
    assert rows[0].row_hash == rows[1].row_hash != rejects[0].row_hash


def test_only_wanted_keys_are_parsed() -> None:
    text = (
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\nA,N1,1,MAIL-IN\nB,N2,X,MAIL-IN\nC,N3,3,MAIL-IN\n"
    )
    keys = list(iter_row_keys(text.encode()))
    batches = parse_roster_batches(text.encode(), day=1, added_in=2, wanted={keys[1], keys[2]})
    parsed = [item for batch in batches for item in (*batch.rows, *batch.rejects)]
    assert sorted((p.row_hash, p.occ) for p in parsed) == sorted(keys[1:])
    assert {p.added_in for p in parsed} == {2}


def test_the_row_hash_is_pinned_and_does_not_depend_on_python_hash_seeds() -> None:
    decoded = b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\nTRAVIS,N1,1,MAIL-IN\n"
    assert list(iter_row_keys(decoded)) == [(GOLDEN_HASH, 0)]
    code = (
        "from texas_turnout_scraper.roster_models import iter_row_keys;"
        "import sys; sys.stdout.write(str(list(iter_row_keys(sys.stdin.buffer.read()))))"
    )
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            input=decoded,
            capture_output=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        assert out == f"[({GOLDEN_HASH}, 0)]".encode()


def test_the_header_is_part_of_every_key() -> None:
    body = "TRAVIS,N1,1,MAIL-IN\n"
    one = list(iter_row_keys(f"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\n{body}".encode()))
    two = list(iter_row_keys(f"COUNTY,VOTER_NAME,ID_VOTER,Voting_Method\n{body}".encode()))
    assert one != two


def test_batches_are_bounded() -> None:
    body = "".join(f"C,N{i},{i},MAIL-IN\n" for i in range(1, 8))
    batches = list(
        parse_roster_batches(
            f"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\n{body}".encode(),
            day=1,
            added_in=1,
            batch_rows=3,
        )
    )
    assert [len(b.rows) + len(b.rejects) for b in batches] == [3, 3, 1]


def test_a_bom_is_not_part_of_the_first_header() -> None:
    rows, _ = _parse(f"﻿COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\nTRAVIS,{NAME},1,MAIL-IN\n")
    assert rows[0].county == "TRAVIS"
    assert rows[0].extra == {}


@pytest.mark.parametrize(
    "header",
    [
        "COUNTY,VOTER_NAME,ID_VOTER,PRECINCT",
        "VOTER_NAME,ID_VOTER,VOTING_METHOD",
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,county",
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,Note,NOTE ",
        "",
    ],
)
def test_a_bad_header_is_a_hard_stop_raised_before_iteration(header: str) -> None:
    decoded = f"{header}\nTRAVIS,{NAME},1,MAIL-IN\n".encode()
    with pytest.raises(RosterSchemaError) as caught:
        parse_roster_batches(decoded, day=1, added_in=1)
    assert NAME not in str(caught.value)
    with pytest.raises(RosterSchemaError):
        check_roster_header(decoded)


def test_schema_errors_are_sync_errors() -> None:
    assert issubclass(RosterSchemaError, EarlyVoteSyncError)


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.mark.parametrize(
    ("model", "constant"),
    [
        (RosterRow, ROW_TABLE_SQL),
        (RosterReject, REJECT_TABLE_SQL),
        (RosterVersion, VERSION_TABLE_SQL),
    ],
)
def test_committed_ddl_matches_what_the_models_generate(model: Any, constant: str) -> None:
    generated = str(
        CreateTable(model.__table__, if_not_exists=True).compile(dialect=sqlite.dialect())
    )
    assert _normalize(constant) == _normalize(generated)


def test_sqlmodel_is_not_a_core_dependency() -> None:
    requirements = importlib.metadata.requires("texas-turnout-scraper") or []
    sqlmodel = [r for r in requirements if r.split(";")[0].strip().startswith("sqlmodel")]
    assert sqlmodel
    assert all(re.search(r"extra == [\"']loader[\"']", r) for r in sqlmodel)


def test_importing_the_sync_module_does_not_import_sqlmodel() -> None:
    code = (
        "import sys, texas_turnout_scraper.early_vote_sync;"
        "assert 'sqlmodel' not in sys.modules, 'sqlmodel';"
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)

"""Roster rows as SQLModel models: validation, full-column capture, quarantine, generated DDL."""

from __future__ import annotations

import importlib.metadata
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
    MARKER_TABLE_SQL,
    REJECT_TABLE_SQL,
    ROW_TABLE_SQL,
    RosterSchemaError,
    check_roster_header,
)
from texas_turnout_scraper.roster_models import (
    RosterReject,
    RosterRow,
    RosterRowsMarker,
    parse_roster_batches,
)

NAME = "ZEBEDIAH QUINCEWORTH"

GOOD: dict[str, Any] = {
    "file_id": "f1",
    "row_id": 0,
    "county": "TRAVIS",
    "voter_name": NAME,
    "id_voter": " 12345 ",
    "voting_method": " mail-in ",
}


def _parse(text: str, **kwargs: int) -> tuple[list[RosterRow], list[RosterReject]]:
    rows: list[RosterRow] = []
    rejects: list[RosterReject] = []
    for batch in parse_roster_batches(text.encode(), "f1", **kwargs):
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
        file_id="f1",
        row_id=0,
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
    assert [row.row_id for row in rows] == [1]
    assert len(rejects) == 1
    reject = rejects[0]
    assert (reject.file_id, reject.row_id) == ("f1", 0)
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
    assert [(r.row_id, r.reason) for r in rejects] == [(0, "column_count"), (1, "column_count")]
    assert rejects[0].raw == {
        "COUNTY": "TRAVIS",
        "VOTER_NAME": NAME,
        "ID_VOTER": "1",
        "VOTING_METHOD": "MAIL-IN",
        "_overflow": ["SURPLUS", "MORE"],
    }
    assert rejects[1].raw == {"COUNTY": "BEXAR", "VOTER_NAME": "SHORT"}


def test_row_ids_count_rejected_lines_and_skip_blank_lines() -> None:
    rows, rejects = _parse(
        "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\n"
        "A,N1,1,MAIL-IN\n"
        "\n"
        "B,N2,X,MAIL-IN\n"
        "C,N3,3,IN-PERSON\n"
    )
    assert [row.row_id for row in rows] == [0, 2]
    assert [reject.row_id for reject in rejects] == [1]


def test_batches_are_bounded() -> None:
    body = "".join(f"C,N{i},{i},MAIL-IN\n" for i in range(1, 8))
    batches = list(
        parse_roster_batches(
            f"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD\n{body}".encode(), "f1", batch_rows=3
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
        parse_roster_batches(decoded, "f1")
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
        (RosterRowsMarker, MARKER_TABLE_SQL),
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

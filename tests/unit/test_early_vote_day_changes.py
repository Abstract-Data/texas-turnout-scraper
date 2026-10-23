"""Added and removed voters when a day's file changes between loads."""

from __future__ import annotations

import sqlite3

from texas_turnout_scraper.early_vote_day_changes import (
    DAY_CHANGE_TABLE_SQL,
    change_statements,
    diff_rosters,
)

_HEADER = "ID_VOTER,VOTER_NAME,COUNTY,VOTING_METHOD\n"


def _csv(*rows: tuple[str, str, str, str]) -> bytes:
    return (_HEADER + "".join(",".join(r) + "\n" for r in rows)).encode()


OLD = _csv(
    ("0000000001", "A ONE", "TRAVIS", "MAIL-IN"),
    ("0000000002", "B TWO", "BEXAR", "IN-PERSON"),
    ("0000000003", "C THREE", "HARRIS", "MAIL-IN"),
)
NEW = _csv(
    ("0000000002", "B TWO", "BEXAR", "IN-PERSON"),
    ("0000000003", "C THREE", "HARRIS", "MAIL-IN"),
    ("0000000004", "D FOUR", "DALLAS", "IN-PERSON"),
)


def test_diff_reports_added_and_removed_voters() -> None:
    diff = diff_rosters(OLD, NEW)
    assert [r.id_voter for r in diff.added] == ["0000000004"]
    assert [r.id_voter for r in diff.removed] == ["0000000001"]
    assert (diff.added[0].county, diff.added[0].voting_method) == ("DALLAS", "IN-PERSON")
    assert (diff.removed[0].county, diff.removed[0].voting_method) == ("TRAVIS", "MAIL-IN")


def test_identical_files_have_no_diff() -> None:
    diff = diff_rosters(OLD, OLD)
    assert not diff.added
    assert not diff.removed


def test_ids_compare_zero_padded_and_stay_strings() -> None:
    old = _csv(("1", "A", "TRAVIS", "MAIL-IN"))
    new = _csv(("0000000001", "A", "TRAVIS", "MAIL-IN"), ("42", "B", "BEXAR", "MAIL-IN"))
    diff = diff_rosters(old, new)
    assert not diff.removed
    assert [r.id_voter for r in diff.added] == ["0000000042"]


def test_turnout_json_payloads_are_not_diffed() -> None:
    assert diff_rosters(b'{"turnout_by_county":[]}', b'{"turnout_by_county":[]}') is None


def test_statements_hold_ids_not_names_and_are_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(DAY_CHANGE_TABLE_SQL)
    diff = diff_rosters(OLD, NEW)
    statements = change_statements(
        "53815", "2026-10-19", old_sha256="old", new_sha256="new", diff=diff, recorded_at="t0"
    )
    for _ in range(2):
        for sql, params in statements:
            conn.execute(sql, params)
    rows = conn.execute(
        "SELECT change_kind, id_voter, county, voting_method FROM early_vote_day_change "
        "ORDER BY change_kind, id_voter"
    ).fetchall()
    assert rows == [
        ("added", "0000000004", "DALLAS", "IN-PERSON"),
        ("removed", "0000000001", "TRAVIS", "MAIL-IN"),
    ]
    columns = [c[1] for c in conn.execute("PRAGMA table_info(early_vote_day_change)")]
    assert "voter_name" not in columns


def test_large_diffs_split_into_statements_within_the_d1_parameter_limit() -> None:
    old = _csv(*[(f"{i:010d}", "N", "TRAVIS", "MAIL-IN") for i in range(1, 501)])
    new = _csv()
    diff = diff_rosters(old, new)
    statements = change_statements(
        "53815", "2026-10-14", old_sha256="o", new_sha256="n", diff=diff, recorded_at="t0"
    )
    assert len(diff.removed) == 500
    assert all(len(params) <= 100 for _, params in statements)
    assert len(statements) > 1

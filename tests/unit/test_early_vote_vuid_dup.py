"""Unit tests for early-vote VUID dup schema constants and compare logic."""

from __future__ import annotations

from texas_turnout_scraper.early_vote_vuid_dup import (
    VUID_DUP_SCHEMA_CREATES,
    EarlyVoteRosterRow,
    EarlyVoteRosterSnapshot,
    compare_early_vote_rosters,
)


def _snap(
    file_id: str,
    rows: tuple[EarlyVoteRosterRow, ...],
    *,
    ev_date: str = "2026-10-20",
) -> EarlyVoteRosterSnapshot:
    return EarlyVoteRosterSnapshot(
        source_election_id="53815",
        ev_date=ev_date,
        file_id=file_id,
        rows=rows,
    )


def test_schema_creates_include_table_indexes_and_view() -> None:
    joined = "\n".join(VUID_DUP_SCHEMA_CREATES)
    assert "CREATE TABLE IF NOT EXISTS early_vote_vuid_dup" in joined
    assert "CHECK (anomaly_kind IN" in joined
    assert "idx_early_vote_vuid_dup_election_voter" in joined
    assert "idx_early_vote_vuid_dup_election_date_kind" in joined
    assert "idx_early_vote_vuid_dup_election_file" in joined
    assert "CREATE VIEW IF NOT EXISTS early_vote_vuid_dup_day" in joined
    assert "dup_excess_rows" in joined
    assert "method_changed_vuids" in joined


def test_within_file_dup_counts_occurrences() -> None:
    current = _snap(
        "file-b",
        (
            EarlyVoteRosterRow("TRAVIS", "0000000001", "IN-PERSON"),
            EarlyVoteRosterRow("TRAVIS", "0000000001", "IN-PERSON"),
            EarlyVoteRosterRow("TRAVIS", "0000000001", "MAIL-IN"),
        ),
    )
    rows = compare_early_vote_rosters(None, current)
    assert len(rows) == 1
    row = rows[0]
    assert row.anomaly_kind == "within_file_dup"
    assert row.occurrence_count == 3
    assert row.prior_voting_method == "MAIL-IN"
    assert row.prior_county is None


def test_dropped_from_later() -> None:
    prior = _snap(
        "file-a",
        (EarlyVoteRosterRow("HARRIS", "0000000002", "MAIL-IN"),),
    )
    current = _snap("file-b", ())
    rows = compare_early_vote_rosters(prior, current)
    assert len(rows) == 1
    assert rows[0].anomaly_kind == "dropped_from_later"
    assert rows[0].prior_file_id == "file-a"
    assert rows[0].prior_county == "HARRIS"


def test_method_and_county_changed() -> None:
    prior = _snap(
        "file-a",
        (EarlyVoteRosterRow("DALLAS", "0000000003", "IN-PERSON"),),
    )
    current = _snap(
        "file-b",
        (EarlyVoteRosterRow("TARRANT", "0000000003", "MAIL-IN"),),
    )
    rows = compare_early_vote_rosters(prior, current)
    kinds = {row.anomaly_kind for row in rows}
    assert kinds == {"method_changed", "county_changed"}


def test_no_cross_file_anomaly_when_unchanged() -> None:
    row = EarlyVoteRosterRow("BEXAR", "0000000004", "IN-PERSON")
    prior = _snap("file-a", (row,))
    current = _snap("file-b", (row,))
    assert compare_early_vote_rosters(prior, current) == ()


def test_mismatched_snapshots_raise() -> None:
    prior = _snap("file-a", (), ev_date="2026-10-19")
    current = _snap("file-b", (), ev_date="2026-10-20")
    try:
        compare_early_vote_rosters(prior, current)
    except ValueError:
        return
    raise AssertionError("expected ValueError for mismatched ev_date")

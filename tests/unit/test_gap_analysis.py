"""Unit tests for turnout vs roster gap analysis."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.gap_analysis import (
    build_turnout_roster_gap_report,
    infer_civix_election_from_roster,
    summarize_roster_by_county,
    write_gap_counties_csv,
    write_gap_report_json,
)
from texas_turnout_scraper.models import CountyTurnout, VoterRecord


def _voter(
    vuid: str,
    *,
    county: str = "HARRIS",
    method: VoteMethod = VoteMethod.IN_PERSON,
) -> VoterRecord:
    return VoterRecord(
        id_voter=vuid,
        voter_name="TEST, VOTER",
        voting_method=method,
        precinct="100",
        county=county,
        election_id="58315",
        report_date=date(2026, 5, 22),
    )


def test_infer_civix_election_from_roster_ev_filename(tmp_path: Path) -> None:
    roster_path = tmp_path / "roster_ev_58315.csv"
    roster_path.write_text("x", encoding="utf-8")
    inferred = infer_civix_election_from_roster(
        roster_path,
        [_voter("0000000001", county="HARRIS")],
    )
    assert inferred == ("58315", tmp_path)


def test_summarize_roster_by_county_mail_only_vs_in_person() -> None:
    records = [
        _voter("0000000001", method=VoteMethod.IN_PERSON),
        _voter("0000000002", method=VoteMethod.MAIL_IN),
        _voter("0000000003", method=VoteMethod.MAIL_IN),
    ]
    summary = summarize_roster_by_county(records)
    assert summary["HARRIS"] == (1, 2, 3)


def test_summarize_roster_cross_method_counts_in_person_bucket() -> None:
    records = [
        _voter("0000000001", method=VoteMethod.IN_PERSON),
        _voter("0000000001", method=VoteMethod.MAIL_IN),
    ]
    summary = summarize_roster_by_county(records)
    assert summary["HARRIS"] == (1, 0, 1)


def test_build_turnout_roster_gap_report_statewide_totals() -> None:
    roster = [
        _voter("0000000001"),
        _voter("0000000002", county="TRAVIS"),
        _voter("0000000003", county="TRAVIS", method=VoteMethod.MAIL_IN),
    ]
    turnout = [
        CountyTurnout(
            election_id="58315",
            report_date=date(2026, 5, 22),
            county="HARRIS",
            county_id=101,
            registered_voters=1000,
            in_person_votes_on_date=10,
            total_in_person_votes=100,
            total_mail_votes=20,
            roster_available=True,
            source="civix",
        ),
        CountyTurnout(
            election_id="58315",
            report_date=date(2026, 5, 22),
            county="TRAVIS",
            county_id=227,
            registered_voters=500,
            in_person_votes_on_date=5,
            total_in_person_votes=50,
            total_mail_votes=10,
            roster_available=True,
            source="civix",
        ),
    ]

    report = build_turnout_roster_gap_report(
        election_id="58315",
        ev_date=date(2026, 5, 22),
        roster_path=Path("data/elections/civix/58315/roster_ev_58315.csv"),
        roster_records=roster,
        turnout_rows=turnout,
        election_name="TEST RUNOFF",
        certified=False,
        turnout_source="stored",
    )

    assert report.turnout_total == 180
    assert report.roster_total == 3
    assert report.gap_total == 177
    assert report.counties_with_gap == 2
    assert report.counties_roster_over_turnout == 0

    harris = next(row for row in report.counties if row.county == "HARRIS")
    assert harris.gap_total == 119
    assert harris.gap_mail == 20


def test_write_gap_report_files(tmp_path: Path) -> None:
    report = build_turnout_roster_gap_report(
        election_id="58315",
        ev_date=date(2026, 5, 22),
        roster_path=tmp_path / "roster.csv",
        roster_records=[_voter("0000000001")],
        turnout_rows=[
            CountyTurnout(
                election_id="58315",
                report_date=date(2026, 5, 22),
                county="HARRIS",
                county_id=101,
                registered_voters=1000,
                in_person_votes_on_date=1,
                total_in_person_votes=10,
                total_mail_votes=0,
                roster_available=True,
                source="civix",
            )
        ],
    )
    json_path = tmp_path / "gap.json"
    csv_path = tmp_path / "gap.csv"
    write_gap_report_json(report, json_path)
    write_gap_counties_csv(report, csv_path)
    assert json_path.exists()
    assert csv_path.exists()
    assert "gap_total" in csv_path.read_text(encoding="utf-8")

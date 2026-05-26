"""Unit tests for models.py."""

from datetime import date

import pytest
from pydantic import ValidationError

from texas_turnout_scraper.enums import ElectionType, VoteMethod
from texas_turnout_scraper.models import (
    AuditReport,
    CivixElection,
    CountyRoster,
    LegacyElection,
    VoterRecord,
)

# ---------------------------------------------------------------------------
# VoterRecord helpers
# ---------------------------------------------------------------------------


def _voter(
    vuid: str = "0123456789",
    method: VoteMethod = VoteMethod.IN_PERSON,
    precinct: str = "100",
    county: str = "HARRIS",
    election_id: str = "53813",
    report_date: date = date(2026, 2, 27),
) -> VoterRecord:
    """Build a minimal VoterRecord for testing."""
    return VoterRecord(
        id_voter=vuid,
        voting_method=method,
        precinct=precinct,
        county=county,
        election_id=election_id,
        report_date=report_date,
    )


# ---------------------------------------------------------------------------
# VoterRecord tests
# ---------------------------------------------------------------------------


def test_voter_record_id_voter_stays_string():
    r = _voter("0123456789")
    assert isinstance(r.id_voter, str)
    assert r.id_voter == "0123456789"


def test_voter_record_leading_zeros_preserved():
    r = _voter("0000000001")
    assert r.id_voter == "0000000001"
    assert r.id_voter != "1"


def test_voter_record_duplicate_defaults():
    r = _voter()
    assert r.duplicate_flag is False
    assert r.duplicate_type == ""
    assert r.also_found_on == ""


def test_voter_record_voter_name_defaults_empty():
    r = _voter()
    assert r.voter_name == ""


def test_voter_record_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        VoterRecord(
            id_voter="0123456789",
            voting_method=VoteMethod.IN_PERSON,
            precinct="100",
            county="HARRIS",
            election_id="53813",
            report_date=date(2026, 2, 27),
            extra_field="not-allowed",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# CountyRoster tests
# ---------------------------------------------------------------------------


def test_county_roster_total_voters():
    records = [
        _voter("0000000001", county="HARRIS"),
        _voter("0000000002", county="HARRIS", method=VoteMethod.MAIL_IN),
    ]
    roster = CountyRoster(
        county="HARRIS",
        county_id=101,
        election_id="53813",
        report_date=date(2026, 2, 27),
        source="civix",
        records=records,
    )
    assert roster.total_voters == 2


def test_county_roster_empty_records():
    roster = CountyRoster(
        county="TRAVIS",
        county_id=227,
        election_id="53813",
        report_date=date(2026, 2, 27),
        source="civix",
    )
    assert roster.total_voters == 0


# ---------------------------------------------------------------------------
# CivixElection tests
# ---------------------------------------------------------------------------


def test_civix_election_parses_mmddyyyy():
    election = CivixElection(
        source_election_id="53813",
        id=53813,
        type="EV",
        election_date="03/03/2026",
        election_name="2026 REPUBLICAN PRIMARY ELECTION",
        certified=True,
        early_voting_dates=[{"date": "02/17/2026", "date_turnout_id": 1}],
        counties=[{"county_id": 1, "name": "ANDERSON"}],
    )
    assert election.election_date == date(2026, 3, 3)
    assert election.election_type == ElectionType.PRIMARY
    assert election.source_election_id == "53813"
    assert isinstance(election.source_election_id, str)


def test_civix_election_id_always_string():
    election = CivixElection(
        source_election_id="53813",
        id=53813,
        type="EV",
        election_date="03/03/2026",
        election_name="2026 REPUBLICAN PRIMARY ELECTION",
        certified=True,
        early_voting_dates=[],
        counties=[],
    )
    assert isinstance(election.source_election_id, str)


# ---------------------------------------------------------------------------
# LegacyElection tests
# ---------------------------------------------------------------------------


def test_legacy_election_infers_type():
    e = LegacyElection(
        source_election_id="49664",
        election_name="2024 NOVEMBER 5TH GENERAL ELECTION",
    )
    assert e.election_type == ElectionType.GENERAL


def test_legacy_special_election_infers_type():
    e = LegacyElection(
        source_election_id="56181",
        election_name="2026 SPECIAL ELECTION SENATE DISTRICT 4",
    )
    assert e.election_type == ElectionType.SPECIAL


# ---------------------------------------------------------------------------
# AuditReport tests
# ---------------------------------------------------------------------------


def test_audit_report_defaults():
    report = AuditReport(
        election_id="53813",
        report_date=date(2026, 2, 27),
        source="civix",
        total_records=1000,
        unique_vuids=998,
        duplicate_vuid_count=2,
        cross_method_duplicate_count=1,
    )
    assert report.findings == []
    assert report.duplicate_vuid_count == 2

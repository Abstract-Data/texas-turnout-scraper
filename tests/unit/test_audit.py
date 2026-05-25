"""Unit tests for audit.py."""
from datetime import date

from texas_turnout_scraper.audit import audit_roster
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import CountyRoster, CountyTurnout, VoterRecord


def _make_roster(county: str, records: list[VoterRecord], report_date: date = date(2026, 2, 27)) -> CountyRoster:
    return CountyRoster(
        county=county, county_id=1, election_id="53813",
        report_date=report_date, source="civix", records=records,
    )


def _voter(
    vuid: str,
    method: VoteMethod = VoteMethod.IN_PERSON,
    county: str = "HARRIS",
    report_date: date = date(2026, 2, 27),
    precinct: str = "100",
) -> VoterRecord:
    return VoterRecord(
        id_voter=vuid,
        voting_method=method,
        precinct=precinct,
        county=county,
        election_id="53813",
        report_date=report_date,
    )


def test_no_duplicates():
    rosters = [_make_roster("HARRIS", [_voter("0000000001"), _voter("0000000002")])]
    report = audit_roster(rosters, election_id="53813", report_date=date(2026, 2, 27), source="civix")
    assert report.duplicate_vuid_count == 0
    assert report.cross_method_duplicate_count == 0
    assert report.total_records == 2
    assert report.unique_vuids == 2


def test_duplicate_vuid_detected():
    rosters = [_make_roster("HARRIS", [_voter("0000000001"), _voter("0000000001")])]
    report = audit_roster(rosters)
    assert report.duplicate_vuid_count >= 1
    findings = [f for f in report.findings if f.finding_type == "duplicate_vuid"]
    assert len(findings) > 0


def test_cross_method_duplicate_detected():
    records = [_voter("0000000001", VoteMethod.IN_PERSON), _voter("0000000001", VoteMethod.MAIL_IN)]
    rosters = [_make_roster("HARRIS", records)]
    report = audit_roster(rosters)
    assert report.cross_method_duplicate_count >= 1
    findings = [f for f in report.findings if f.finding_type == "cross_method_duplicate"]
    assert len(findings) > 0


def test_missing_county_finding():
    rosters = [_make_roster("HARRIS", [_voter("0000000001")])]
    turnout = [
        CountyTurnout(
            election_id="53813", report_date=date(2026, 2, 27),
            county="HARRIS", county_id=101, registered_voters=5000,
            in_person_votes_on_date=100, total_in_person_votes=100,
            total_mail_votes=0, source="civix",
        ),
        CountyTurnout(
            election_id="53813", report_date=date(2026, 2, 27),
            county="TRAVIS", county_id=227, registered_voters=3000,
            in_person_votes_on_date=50, total_in_person_votes=50,
            total_mail_votes=0, source="civix",
        ),
    ]
    report = audit_roster(rosters, turnout_summary=turnout)
    missing = [f for f in report.findings if f.finding_type == "missing_county"]
    assert len(missing) == 1
    assert "TRAVIS" in missing[0].detail


def test_no_pii_in_findings():
    """Verify no VUID values appear in any finding detail."""
    records = [_voter("9876543210", VoteMethod.IN_PERSON), _voter("9876543210", VoteMethod.MAIL_IN)]
    rosters = [_make_roster("HARRIS", records)]
    report = audit_roster(rosters)
    for finding in report.findings:
        assert "9876543210" not in finding.detail

"""Unit tests for writer.py — accumulate_roster, CSV I/O, audit_records."""

from __future__ import annotations

import csv
import tempfile
from datetime import date
from pathlib import Path

import pytest

from texas_turnout_scraper.audit import audit_records
from texas_turnout_scraper.enums import FindingType, VoteMethod
from texas_turnout_scraper.models import CountyRoster, VoterRecord
from texas_turnout_scraper.writer import (
    ROSTER_CSV_COLUMNS,
    accumulate_roster,
    read_roster_csv,
    report_date_from_roster_csv,
    roster_csv_to_text,
    write_roster_csv,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_D1 = date(2026, 2, 17)
_D2 = date(2026, 2, 27)
_EID = "53813"
_SYNTHETIC_NAME_A = "DOE, JOHN A"
_SYNTHETIC_NAME_B = "DOE, JANE A"


def _rec(
    vuid: str,
    method: VoteMethod = VoteMethod.IN_PERSON,
    county: str = "HARRIS",
    report_date: date = _D1,
    precinct: str = "100",
    voter_name: str = "",
) -> VoterRecord:
    return VoterRecord(
        id_voter=vuid,
        voting_method=method,
        precinct=precinct,
        county=county,
        election_id=_EID,
        report_date=report_date,
        voter_name=voter_name,
    )


def _roster(county: str, records: list[VoterRecord], report_date: date = _D1) -> CountyRoster:
    return CountyRoster(
        county=county,
        county_id=101,
        election_id=_EID,
        report_date=report_date,
        source="civix",
        records=records,
    )


def _duplicate_flags_from_csv(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [row["DUPLICATE_FLAG"] for row in csv.DictReader(fh)]


# ---------------------------------------------------------------------------
# accumulate_roster — basic
# ---------------------------------------------------------------------------


def test_accumulate_returns_all_records():
    r1 = _roster("HARRIS", [_rec("0000000001"), _rec("0000000002")])
    result = accumulate_roster([r1])
    assert len(result) == 2


def test_accumulate_empty_rosters():
    assert accumulate_roster([]) == []


def test_accumulate_roster_with_empty_records():
    assert accumulate_roster([_roster("HARRIS", [])]) == []


def test_accumulate_no_duplicate_flag_for_unique_vuids():
    rosters = [_roster("HARRIS", [_rec("0000000001"), _rec("0000000002")])]
    result = accumulate_roster(rosters)
    for rec in result:
        assert rec.duplicate_flag is False
        assert rec.duplicate_type == ""
        assert rec.also_found_on == ""


# ---------------------------------------------------------------------------
# accumulate_roster — multiple_dates flag
# ---------------------------------------------------------------------------


def test_multiple_dates_flagged():
    """Same VUID on two different report dates → multiple_dates flag."""
    rosters = [
        _roster("HARRIS", [_rec("1111111111", report_date=_D1)], report_date=_D1),
        _roster("HARRIS", [_rec("1111111111", report_date=_D2)], report_date=_D2),
    ]
    result = accumulate_roster(rosters)
    assert all(rec.duplicate_flag for rec in result)
    assert all("multiple_dates" in rec.duplicate_type for rec in result)


# ---------------------------------------------------------------------------
# accumulate_roster — conflicting_method flag
# ---------------------------------------------------------------------------


def test_conflicting_method_flagged():
    """Same VUID with IN_PERSON + MAIL_IN → conflicting_method flag."""
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("2222222222", method=VoteMethod.IN_PERSON),
                _rec("2222222222", method=VoteMethod.MAIL_IN),
            ],
        )
    ]
    result = accumulate_roster(rosters)
    assert all(rec.duplicate_flag for rec in result)
    assert all("conflicting_method" in rec.duplicate_type for rec in result)


def test_also_found_on_same_county_and_date_duplicates():
    """Same VUID twice on one county/date still populates also_found_on."""
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("2222222222", method=VoteMethod.IN_PERSON),
                _rec("2222222222", method=VoteMethod.MAIL_IN),
            ],
        )
    ]
    result = accumulate_roster(rosters)
    assert len(result) == 2
    for rec in result:
        assert rec.also_found_on == "HARRIS|2026-02-17"


# ---------------------------------------------------------------------------
# accumulate_roster — multiple_counties flag
# ---------------------------------------------------------------------------


def test_multiple_counties_flagged():
    """Same VUID in two different counties → multiple_counties flag."""
    rosters = [
        _roster("HARRIS", [_rec("3333333333", county="HARRIS")]),
        _roster("TRAVIS", [_rec("3333333333", county="TRAVIS")]),
    ]
    result = accumulate_roster(rosters)
    assert all(rec.duplicate_flag for rec in result)
    assert all("multiple_counties" in rec.duplicate_type for rec in result)


# ---------------------------------------------------------------------------
# accumulate_roster — name_mismatch flag
# ---------------------------------------------------------------------------


def test_name_mismatch_flagged():
    """Same VUID with different voter names → name_mismatch flag."""
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("4444444444", voter_name=_SYNTHETIC_NAME_A),
                _rec("4444444444", voter_name=_SYNTHETIC_NAME_B),
            ],
        )
    ]
    result = accumulate_roster(rosters)
    assert all(rec.duplicate_flag for rec in result)
    assert all("name_mismatch" in rec.duplicate_type for rec in result)


def test_name_mismatch_empty_vs_nonempty():
    """Empty VOTER_NAME vs populated name on same VUID → name_mismatch."""
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("4444444445", voter_name=""),
                _rec("4444444445", voter_name=_SYNTHETIC_NAME_A),
            ],
        )
    ]
    result = accumulate_roster(rosters)
    assert all("name_mismatch" in rec.duplicate_type for rec in result)


def test_name_mismatch_not_flagged_when_both_empty():
    rosters = [
        _roster("HARRIS", [_rec("4444444446", voter_name=""), _rec("4444444446", voter_name="")])
    ]
    result = accumulate_roster(rosters)
    assert all("name_mismatch" not in rec.duplicate_type for rec in result)


# ---------------------------------------------------------------------------
# accumulate_roster — precinct_mismatch flag
# ---------------------------------------------------------------------------


def test_precinct_mismatch_flagged():
    """Same VUID in two different precincts → precinct_mismatch flag."""
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("5555555555", precinct="100"),
                _rec("5555555555", precinct="200"),
            ],
        )
    ]
    result = accumulate_roster(rosters)
    assert all(rec.duplicate_flag for rec in result)
    assert all("precinct_mismatch" in rec.duplicate_type for rec in result)


# ---------------------------------------------------------------------------
# accumulate_roster — also_found_on
# ---------------------------------------------------------------------------


def test_also_found_on_contains_other_appearances():
    rosters = [
        _roster("HARRIS", [_rec("6666666666", county="HARRIS", report_date=_D1)], report_date=_D1),
        _roster("TRAVIS", [_rec("6666666666", county="TRAVIS", report_date=_D2)], report_date=_D2),
    ]
    result = accumulate_roster(rosters)
    harris_row = next(r for r in result if r.county == "HARRIS")
    travis_row = next(r for r in result if r.county == "TRAVIS")
    assert "HARRIS|2026-02-17" not in harris_row.also_found_on
    assert "TRAVIS|2026-02-27" in harris_row.also_found_on
    assert "TRAVIS|2026-02-27" not in travis_row.also_found_on
    assert "HARRIS|2026-02-17" in travis_row.also_found_on


def test_also_found_on_three_appearances_deduped():
    rosters = [
        _roster("HARRIS", [_rec("6666666667", report_date=_D1)], report_date=_D1),
        _roster("TRAVIS", [_rec("6666666667", county="TRAVIS", report_date=_D2)], report_date=_D2),
        _roster("DALLAS", [_rec("6666666667", county="DALLAS", report_date=_D2)], report_date=_D2),
    ]
    result = accumulate_roster(rosters)
    harris_row = next(r for r in result if r.county == "HARRIS")
    assert harris_row.also_found_on == "TRAVIS|2026-02-27; DALLAS|2026-02-27"


# ---------------------------------------------------------------------------
# accumulate_roster — multiple flags at once
# ---------------------------------------------------------------------------


def test_multiple_flags_combined():
    """A VUID appearing on two dates with conflicting methods gets both flags."""
    rosters = [
        _roster(
            "HARRIS",
            [_rec("7777777777", method=VoteMethod.IN_PERSON, report_date=_D1)],
            report_date=_D1,
        ),
        _roster(
            "HARRIS",
            [_rec("7777777777", method=VoteMethod.MAIL_IN, report_date=_D2)],
            report_date=_D2,
        ),
    ]
    result = accumulate_roster(rosters)
    for rec in result:
        assert "multiple_dates" in rec.duplicate_type
        assert "conflicting_method" in rec.duplicate_type


def test_accumulate_does_not_mutate_input_records():
    """Input VoterRecord objects must remain unchanged after accumulate_roster."""
    original = _rec("8888888888", voter_name=_SYNTHETIC_NAME_A)
    snapshot = (
        original.id_voter,
        original.voting_method,
        original.precinct,
        original.county,
        original.election_id,
        original.report_date,
        original.voter_name,
        original.duplicate_flag,
        original.duplicate_type,
        original.also_found_on,
    )
    rosters = [
        _roster("HARRIS", [original, _rec("8888888888", method=VoteMethod.MAIL_IN)]),
    ]
    accumulate_roster(rosters)
    assert (
        original.id_voter,
        original.voting_method,
        original.precinct,
        original.county,
        original.election_id,
        original.report_date,
        original.voter_name,
        original.duplicate_flag,
        original.duplicate_type,
        original.also_found_on,
    ) == snapshot


# ---------------------------------------------------------------------------
# CSV round-trip
# ---------------------------------------------------------------------------


def test_write_and_read_roster_csv_roundtrip():
    records = [
        _rec("0000000001", voter_name=_SYNTHETIC_NAME_A),
        _rec("0000000002", method=VoteMethod.MAIL_IN, voter_name=_SYNTHETIC_NAME_B),
    ]
    flagged = accumulate_roster([_roster("HARRIS", records)])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roster.csv"
        write_roster_csv(flagged, path)
        assert path.exists()
        reloaded = read_roster_csv(path)
    assert len(reloaded) == 2
    assert reloaded[0].id_voter == "0000000001"
    assert isinstance(reloaded[0].id_voter, str)
    assert reloaded[0].voter_name == _SYNTHETIC_NAME_A
    assert reloaded[1].voting_method == VoteMethod.MAIL_IN


def test_csv_roundtrip_preserves_all_fields():
    """write_roster_csv → read_roster_csv preserves every VoterRecord field."""
    records = [
        _rec(
            "0000000042",
            method=VoteMethod.MAIL_IN,
            county="TRAVIS",
            precinct="42",
            voter_name=_SYNTHETIC_NAME_A,
        ),
        _rec("0000000042", method=VoteMethod.IN_PERSON, county="HARRIS", precinct="99"),
    ]
    flagged = accumulate_roster([_roster("HARRIS", records)])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roster.csv"
        write_roster_csv(flagged, path)
        reloaded = read_roster_csv(path)
    for orig, back in zip(flagged, reloaded, strict=True):
        assert back.voter_name == orig.voter_name
        assert back.id_voter == orig.id_voter
        assert back.voting_method == orig.voting_method
        assert back.precinct == orig.precinct
        assert back.county == orig.county
        assert back.election_id == orig.election_id
        assert back.report_date == orig.report_date
        assert back.duplicate_flag == orig.duplicate_flag
        assert back.duplicate_type == orig.duplicate_type
        assert back.also_found_on == orig.also_found_on


def test_duplicate_flag_serializes_lowercase_in_csv():
    records = [
        _rec("0000000001"),
        _rec("0000000001", method=VoteMethod.MAIL_IN),
    ]
    flagged = accumulate_roster([_roster("HARRIS", records)])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roster.csv"
        write_roster_csv(flagged, path)
        flags = _duplicate_flags_from_csv(path)
    assert flags == ["true", "true"]


def test_read_roster_csv_rejects_invalid_voting_method():
    records = [_rec("0000000001")]
    flagged = accumulate_roster([_roster("HARRIS", records)])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roster.csv"
        write_roster_csv(flagged, path)
        raw = path.read_text(encoding="utf-8").replace("IN-PERSON", "NOT-A-METHOD", 1)
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid VOTING_METHOD"):
            read_roster_csv(path)


def test_read_roster_csv_rejects_missing_columns():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "bad.csv"
        path.write_text("ID_VOTER\n1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required columns"):
            read_roster_csv(path)


def test_roster_csv_to_text_contains_header():
    records = [_rec("0000000001")]
    flagged = accumulate_roster([_roster("HARRIS", records)])
    text = roster_csv_to_text(flagged)
    header_line = text.splitlines()[0]
    assert next(iter(csv.reader([header_line]))) == list(ROSTER_CSV_COLUMNS)


def test_id_voter_leading_zeros_preserved_in_csv():
    records = [_rec("0000000001")]
    flagged = accumulate_roster([_roster("HARRIS", records)])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roster.csv"
        write_roster_csv(flagged, path)
        reloaded = read_roster_csv(path)
    assert reloaded[0].id_voter == "0000000001"


def test_read_roster_csv_zfills_unpadded_vuid():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "roster.csv"
        path.write_text(
            '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT","COUNTY",'
            '"ELECTION_ID","REPORT_DATE","DUPLICATE_FLAG","DUPLICATE_TYPE","ALSO_FOUND_ON"\n'
            '"DOE, JOHN","12345","IN-PERSON","1","HARRIS","53813","2026-02-17","false","",""\n',
            encoding="utf-8",
        )
        reloaded = read_roster_csv(path)
    assert reloaded[0].id_voter == "0000012345"
    assert isinstance(reloaded[0].id_voter, str)


# ---------------------------------------------------------------------------
# audit_records
# ---------------------------------------------------------------------------


def test_audit_records_counts_correctly():
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("8888888888", method=VoteMethod.IN_PERSON),
                _rec("8888888888", method=VoteMethod.MAIL_IN),
                _rec("9999999999"),
            ],
        )
    ]
    flagged = accumulate_roster(rosters)
    report = audit_records(flagged, election_id=_EID, report_date=_D1, source="civix")
    assert report.total_records == 3
    assert report.unique_vuids == 2
    assert report.duplicate_vuid_count == 1
    assert report.cross_method_duplicate_count == 1


def test_audit_records_findings_non_empty_for_duplicates():
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("8888888888", method=VoteMethod.IN_PERSON),
                _rec("8888888888", method=VoteMethod.MAIL_IN),
            ],
        )
    ]
    flagged = accumulate_roster(rosters)
    report = audit_records(flagged, election_id=_EID, report_date=_D1, source="civix")
    assert len(report.findings) > 0
    finding_types = {f.finding_type for f in report.findings}
    assert "conflicting_method" in finding_types


def test_audit_records_empty_records():
    report = audit_records([], election_id="49664", report_date=_D1, source="legacy")
    assert report.election_id == "49664"
    assert report.report_date == _D1
    assert report.source == "legacy"
    assert report.total_records == 0
    assert report.unique_vuids == 0
    assert report.duplicate_vuid_count == 0
    assert report.cross_method_duplicate_count == 0
    assert report.findings == []


def test_audit_records_overrides_election_id_and_report_date():
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("8888888888", method=VoteMethod.IN_PERSON),
                _rec("8888888888", method=VoteMethod.MAIL_IN),
            ],
        )
    ]
    flagged = accumulate_roster(rosters)
    override_date = date(2024, 10, 21)
    report = audit_records(
        flagged,
        election_id="49664",
        report_date=override_date,
        source="legacy",
    )
    assert report.election_id == "49664"
    assert report.report_date == override_date
    assert report.source == "legacy"


def test_audit_records_detects_precinct_mismatch_from_data():
    """audit_records re-detects mismatches from row data, not duplicate_type flags."""
    records = [
        _rec("0000000099", precinct="1"),
        _rec("0000000099", precinct="2"),
    ]
    report = audit_records(records)
    finding_types = {f.finding_type for f in report.findings}
    assert FindingType.PRECINCT_MISMATCH.value in finding_types


def test_audit_records_no_pii_in_findings():
    """Finding details must not contain VUIDs or voter name literals."""
    rosters = [
        _roster(
            "HARRIS",
            [
                _rec("1234567890", method=VoteMethod.IN_PERSON, voter_name=_SYNTHETIC_NAME_A),
                _rec("1234567890", method=VoteMethod.MAIL_IN, voter_name=_SYNTHETIC_NAME_B),
            ],
        )
    ]
    flagged = accumulate_roster(rosters)
    report = audit_records(flagged, election_id=_EID, report_date=_D1, source="civix")
    for finding in report.findings:
        assert "1234567890" not in finding.detail
        assert _SYNTHETIC_NAME_A not in finding.detail
        assert _SYNTHETIC_NAME_B not in finding.detail


def test_report_date_from_roster_csv_empty_uses_utc_today(tmp_path: Path) -> None:
    """Header-only roster CSV falls back to UTC today (audit run without ev_date)."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    fixed = date(2026, 5, 27)
    csv_path = tmp_path / "roster_ev_58315.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ROSTER_CSV_COLUMNS)

    with patch("texas_turnout_scraper.writer.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(
            2026,
            5,
            27,
            12,
            0,
            0,
            tzinfo=timezone.utc,
        )
        assert report_date_from_roster_csv(csv_path) == fixed

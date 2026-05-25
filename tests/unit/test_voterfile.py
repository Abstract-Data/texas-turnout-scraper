"""Unit tests for texas_turnout_scraper.voterfile.

Covers:
- Column auto-detection (exact, pattern, prefix, undetected)
- Age bracket calculation (all ranges, DOB formats, blank/null inputs)
- Match logic (basic match, unmatched records, VUID zero-padding,
  county mismatch, report counts, age breakdown, PII guard in findings)
- CSV round-trip for enriched records
- Mapping persistence (save/load JSON sidecar)

DuckDB-dependent tests (match_voterfile_to_roster) use the
tests/fixtures/voterfiles/sample_voterfile.csv fixture file directly
and are skipped automatically if duckdb is not installed.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import ColumnMapping, EnrichedVoterRecord, VoterRecord
from texas_turnout_scraper.voterfile import (
    _quote_sql_identifier,
    age_bracket,
    detect_columns,
    load_mapping,
    mapping_column_conflicts,
    match_voterfile_to_roster,
    normalize_precinct,
    normalize_vuid,
    precincts_match,
    save_mapping,
    write_enriched_csv,
)

# ---------------------------------------------------------------------------
# DuckDB availability — gate match tests without killing the whole module
# ---------------------------------------------------------------------------

try:
    import duckdb as _duckdb  # noqa: F401
    _HAS_DUCKDB = True
except ImportError:
    _HAS_DUCKDB = False

requires_duckdb = pytest.mark.skipif(
    not _HAS_DUCKDB,
    reason="duckdb not installed — run `uv sync` or `pip install duckdb`",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "voterfiles"
SAMPLE_VOTERFILE = FIXTURE_DIR / "sample_voterfile.csv"

# Fixed reference date so age-bracket tests are deterministic regardless of
# when the test suite runs.  All fixture DOBs were chosen against this date.
_REF = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _voter(
    vuid: str = "0000000001",
    method: VoteMethod = VoteMethod.IN_PERSON,
    precinct: str = "0510",
    county: str = "HARRIS",
    election_id: str = "12345",
    report_date: date = date(2026, 5, 20),
    voter_name: str = "",
) -> VoterRecord:
    return VoterRecord(
        id_voter=vuid,
        voting_method=method,
        precinct=precinct,
        county=county,
        election_id=election_id,
        report_date=report_date,
        voter_name=voter_name,
    )


def _enriched(
    vuid: str = "0000000001",
    in_voterfile: bool = True,
    age_bracket_val: str | None = "35-44",
) -> EnrichedVoterRecord:
    return EnrichedVoterRecord(
        id_voter=vuid,
        voting_method=VoteMethod.IN_PERSON,
        precinct="0510",
        county="HARRIS",
        election_id="12345",
        report_date=date(2026, 5, 20),
        in_voterfile=in_voterfile,
        age_bracket=age_bracket_val,
    )


def _write_csv(tmp_path: Path, rows: list[dict], filename: str = "voterfile.csv") -> Path:
    """Write a minimal voterfile CSV for column-detection tests."""
    out = tmp_path / filename
    if not rows:
        out.write_text("", encoding="utf-8")
        return out
    fieldnames = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out


# ---------------------------------------------------------------------------
# Column detection tests
# ---------------------------------------------------------------------------


class TestDetectColumnsExactMatch:
    def test_vuid_exact(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS", "PCT": "0510",
                                       "DOB": "19850315", "SEX": "M", "STATUS": "V",
                                       "HISPANIC": "N"}])
        mapping, confidence = detect_columns(path)
        assert mapping.vuid == "VUID"
        assert confidence["vuid"] == "✓ Exact"

    def test_county_exact(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS"}])
        mapping, _ = detect_columns(path)
        assert mapping.county == "COUNTY"

    def test_precinct_pct_exact(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS", "PCT": "0510"}])
        mapping, confidence = detect_columns(path)
        assert mapping.precinct == "PCT"
        # PCT is in the pattern list for precinct
        assert confidence["precinct"] in ("✓ Exact", "✓ Pattern")

    def test_sex_exact(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "SEX": "M"}])
        mapping, confidence = detect_columns(path)
        assert mapping.sex == "SEX"
        assert confidence["sex"] == "✓ Exact"

    def test_status_exact(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "STATUS": "V"}])
        mapping, confidence = detect_columns(path)
        assert mapping.status == "STATUS"
        assert confidence["status"] == "✓ Exact"


class TestDetectColumnsPrefixPattern:
    def test_cd_prefix_planc(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "CDPLANC2333": "7",
                                       "HD2022": "145", "SD2022": "6"}])
        mapping, confidence = detect_columns(path)
        assert mapping.cd == "CDPLANC2333"
        assert confidence["cd"] == "~ Prefix"

    def test_hd_prefix_year(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "CDPLANC2333": "7",
                                       "HD2022": "145", "SD2022": "6"}])
        mapping, confidence = detect_columns(path)
        assert mapping.hd == "HD2022"
        assert confidence["hd"] == "~ Prefix"

    def test_sd_prefix_year(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "CDPLANC2333": "7",
                                       "HD2022": "145", "SD2022": "6"}])
        mapping, confidence = detect_columns(path)
        assert mapping.sd == "SD2022"
        assert confidence["sd"] == "~ Prefix"

    def test_sample_voterfile_full_detection(self):
        """Detect all expected columns from the sample fixture file."""
        mapping, _confidence = detect_columns(SAMPLE_VOTERFILE)
        assert mapping.vuid == "VUID"
        assert mapping.cd == "CDPLANC2333"
        assert mapping.hd == "HD2022"
        assert mapping.sd == "SD2022"
        assert mapping.county == "COUNTY"
        assert mapping.precinct == "PCT"
        assert mapping.last_name == "LNAME"
        assert mapping.first_name == "FNAME"
        assert mapping.dob == "DOB"
        assert mapping.sex == "SEX"
        assert mapping.hispanic == "HISPANIC"
        assert mapping.status == "STATUS"


class TestDetectColumnsAlternateNames:
    def test_van_style_columns(self, tmp_path):
        path = _write_csv(tmp_path, [{"VANID": "1000", "LAST": "DOE",
                                       "FIRST": "JOHN", "BIRTH_DATE": "19850315"}])
        mapping, confidence = detect_columns(path)
        # VAN's VANID maps to vuid pattern list
        assert mapping.vuid == "VANID"
        assert confidence["vuid"] == "✓ Pattern"

    def test_l2_style_lname_fname(self, tmp_path):
        path = _write_csv(tmp_path, [{"LALVOTERID": "abc", "LNAME": "DOE",
                                       "FNAME": "JANE"}])
        mapping, _ = detect_columns(path)
        assert mapping.vuid == "LALVOTERID"
        assert mapping.last_name == "LNAME"
        assert mapping.first_name == "FNAME"

    def test_dob_birthdate_alias(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "BIRTHDATE": "19850315"}])
        mapping, confidence = detect_columns(path)
        assert mapping.dob == "BIRTHDATE"
        assert confidence["dob"] == "✓ Pattern"


class TestDetectColumnsUndetectedField:
    def test_no_district_columns(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS"}])
        mapping, confidence = detect_columns(path)
        assert mapping.cd is None
        assert mapping.hd is None
        assert mapping.sd is None
        assert confidence["cd"] == "✗ Not detected"
        assert confidence["hd"] == "✗ Not detected"
        assert confidence["sd"] == "✗ Not detected"

    def test_no_dob_column(self, tmp_path):
        path = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS"}])
        mapping, confidence = detect_columns(path)
        assert mapping.dob is None
        assert confidence["dob"] == "✗ Not detected"

    def test_county_prefix_not_confused_with_cd(self, tmp_path):
        """COUNTY must not be detected as CD (different prefix)."""
        path = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS"}])
        mapping, _ = detect_columns(path)
        # cd should NOT be "COUNTY"
        assert mapping.cd != "COUNTY"


# ---------------------------------------------------------------------------
# Age bracket tests
# ---------------------------------------------------------------------------


class TestAgeBracket18_24:
    def test_age_22(self):
        # Born 2004-06-01 → exactly 22 on 2026-06-01
        assert age_bracket("20040601", _REF) == "18-24"

    def test_age_18_lower_bound(self):
        assert age_bracket("20080601", _REF) == "18-24"

    def test_age_24_upper_bound(self):
        assert age_bracket("20020601", _REF) == "18-24"


class TestAgeBracketYYYYMMDDFormat:
    def test_texas_state_format_yyyymmdd(self):
        # DOB 19850315 → born March 15, 1985 → 41 on ref date 2026-06-01
        result = age_bracket("19850315", _REF)
        assert result == "35-44"

    def test_iso_format_yyyy_mm_dd(self):
        result = age_bracket("1985-03-15", _REF)
        assert result == "35-44"

    def test_us_format_mm_slash_dd_slash_yyyy(self):
        result = age_bracket("03/15/1985", _REF)
        assert result == "35-44"


@pytest.mark.parametrize("age,expected", [
    (18, "18-24"),
    (24, "18-24"),
    (25, "25-34"),
    (34, "25-34"),
    (35, "35-44"),
    (44, "35-44"),
    (45, "45-54"),
    (54, "45-54"),
    (55, "55-64"),
    (64, "55-64"),
    (65, "65-74"),
    (74, "65-74"),
    (75, "75+"),
    (90, "75+"),
])
def test_age_bracket_all_ranges(age: int, expected: str):
    """Build DOB from (reference_date - age years) and assert bracket."""
    # Use Jan 1 of (ref_year - age) so birthday has passed by ref date (June 1)
    dob = date(_REF.year - age, 1, 1)
    result = age_bracket(dob.strftime("%Y%m%d"), _REF)
    assert result == expected, f"age={age}: expected {expected!r}, got {result!r}"


class TestNormalizeVuid:
    def test_pads_short_vuid(self):
        assert normalize_vuid("1000001") == "0001000001"

    def test_truncates_long_vuid_to_last_ten_digits(self):
        assert normalize_vuid("12345678901") == "2345678901"

    def test_strips_non_digits(self):
        assert normalize_vuid(" 0000000001 ") == "0000000001"


class TestNormalizePrecinct:
    def test_unpadded_roster_matches_zero_padded_voterfile(self):
        assert normalize_precinct("510") == normalize_precinct("0510")

    def test_precincts_match_helper(self):
        assert precincts_match("510", "0510")
        assert precincts_match("0510", "0510")

    def test_different_precincts_do_not_match(self):
        assert not precincts_match("510", "0512")

    def test_overpadded_leading_zeros(self):
        assert normalize_precinct("00510") == normalize_precinct("0510")

    def test_label_with_digits_matches_padded(self):
        assert precincts_match("PCT-510", "0510")

    def test_empty_returns_empty(self):
        assert normalize_precinct("") == ""
        assert normalize_precinct("   ") == ""


class TestMappingColumnConflicts:
    def test_detects_duplicate_column_assignment(self):
        mapping = ColumnMapping(vuid="VUID", cd="VUID", hd="HD2022")
        conflicts = mapping_column_conflicts(mapping)
        assert len(conflicts) == 1
        assert "VUID" in conflicts[0]

    def test_no_conflict_when_columns_distinct(self):
        mapping = ColumnMapping(vuid="VUID", cd="CDPLANC2333", hd="HD2022")
        assert mapping_column_conflicts(mapping) == []


class TestAgeBracketBlankReturnsNone:
    def test_empty_string(self):
        assert age_bracket("") is None

    def test_null_string(self):
        assert age_bracket("NULL") is None

    def test_none_value(self):
        assert age_bracket(None) is None  # type: ignore[arg-type]

    def test_whitespace_only(self):
        assert age_bracket("   ") is None

    def test_invalid_format(self):
        assert age_bracket("not-a-date") is None


# ---------------------------------------------------------------------------
# SQL column name sanitization
# ---------------------------------------------------------------------------


class TestQuoteSqlIdentifier:
    def test_empty_column_name_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _quote_sql_identifier("")

    def test_embedded_double_quotes_are_escaped(self):
        assert _quote_sql_identifier('VUID"X') == '"VUID""X"'

    def test_spaces_preserved_in_identifier(self):
        assert _quote_sql_identifier("VOTER ID") == '"VOTER ID"'


@requires_duckdb
class TestMatchVoterfileSqlColumnSanitization:
    def test_column_name_with_embedded_quote_does_not_break_sql(self, tmp_path):
        """A header containing a double quote must be escaped, not break SQL."""
        vf = tmp_path / "quoted_header.csv"
        vf.write_text(
            '"VUID""","COUNTY"\n"0000000001","HARRIS"\n',
            encoding="utf-8",
        )
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid='VUID"', county="COUNTY")
        enriched, report = match_voterfile_to_roster(
            roster, vf, mapping, reference_date=_REF
        )
        assert enriched[0].in_voterfile is True
        assert report.matched_count == 1

    def test_empty_vuid_column_name_raises(self, tmp_path):
        vf = _write_csv(tmp_path, [{"VUID": "1", "COUNTY": "HARRIS"}])
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="")
        with pytest.raises(ValueError, match="must not be empty"):
            match_voterfile_to_roster(roster, vf, mapping, reference_date=_REF)


# ---------------------------------------------------------------------------
# Match logic tests (DuckDB — skipped if duckdb not installed)
# ---------------------------------------------------------------------------


@requires_duckdb
class TestMatchVoterfileToRosterBasic:
    def test_three_records_all_matched(self):
        roster = [
            _voter("0000000001", precinct="0510"),
            _voter("0000000002", precinct="0510"),
            _voter("0000000003", precinct="0512"),
        ]
        mapping = ColumnMapping(
            vuid="VUID", cd="CDPLANC2333", hd="HD2022", sd="SD2022",
            county="COUNTY", precinct="PCT", dob="DOB", sex="SEX",
            hispanic="HISPANIC", status="STATUS",
            last_name="LNAME", first_name="FNAME",
        )
        enriched, _report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert len(enriched) == 3
        matched = [r for r in enriched if r.in_voterfile]
        assert len(matched) == 3

    def test_matched_records_have_district_fields(self):
        roster = [_voter("0000000001", precinct="0510")]
        mapping = ColumnMapping(
            vuid="VUID", cd="CDPLANC2333", hd="HD2022", sd="SD2022",
            county="COUNTY", precinct="PCT", dob="DOB",
        )
        enriched, _ = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        r = enriched[0]
        assert r.in_voterfile is True
        assert r.cd == "7"
        assert r.hd == "145"
        assert r.sd == "6"

    def test_matched_record_has_age_bracket(self):
        # VUID 0000000001: DOB 19850315 → 41 on 2026-06-01 → "35-44"
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="VUID", dob="DOB")
        enriched, _ = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert enriched[0].age_bracket == "35-44"

    def test_id_voter_stays_string_with_leading_zeros(self):
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="VUID")
        enriched, _ = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        for r in enriched:
            assert isinstance(r.id_voter, str)


@requires_duckdb
class TestMatchVoterfileUnmatchedRecords:
    def test_unknown_vuid_not_in_voterfile(self):
        roster = [_voter("9999999999")]  # not in fixture
        mapping = ColumnMapping(vuid="VUID")
        enriched, _report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert enriched[0].in_voterfile is False
        assert enriched[0].cd is None
        assert enriched[0].hd is None
        assert enriched[0].age_bracket is None

    def test_unmatched_finding_present(self):
        roster = [_voter("9999999999")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        finding_types = [f.finding_type for f in report.findings]
        assert "unmatched_voters" in finding_types

    def test_partial_match_unmatched_finding(self):
        roster = [_voter("0000000001"), _voter("9999999999")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert report.matched_count == 1
        assert report.unmatched_count == 1
        assert "unmatched_voters" in [f.finding_type for f in report.findings]


@requires_duckdb
class TestMatchVoterfileVuidZeroPadding:
    def test_short_vuid_in_voterfile_matches_padded_roster(self, tmp_path):
        """Voterfile row with VUID '1000001' (no leading zeros) must match
        a roster entry with id_voter '0001000001' (10-digit padded)."""
        # Create a one-row voterfile with short VUID
        vf = tmp_path / "short_vuid.csv"
        vf.write_text(
            '"VUID","COUNTY","DOB"\n"1000001","HARRIS","19850315"\n',
            encoding="utf-8",
        )
        roster = [_voter("0001000001")]  # 10-digit padded
        mapping = ColumnMapping(vuid="VUID", dob="DOB")
        enriched, report = match_voterfile_to_roster(
            roster, vf, mapping, reference_date=_REF
        )
        assert enriched[0].in_voterfile is True
        assert report.matched_count == 1


@requires_duckdb
class TestMatchReportCounts:
    def test_match_rate_three_of_five(self):
        roster = [
            _voter("0000000001"),
            _voter("0000000002"),
            _voter("0000000003"),
            _voter("9999999991"),  # not in fixture
            _voter("9999999992"),  # not in fixture
        ]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert report.matched_count == 3
        assert report.unmatched_count == 2
        assert report.total_roster_records == 5
        assert abs(report.match_rate - 0.6) < 1e-9

    def test_all_matched_rate_is_one(self):
        roster = [_voter("0000000001"), _voter("0000000002")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert report.match_rate == 1.0
        assert report.unmatched_count == 0
        assert not any(f.finding_type == "unmatched_voters" for f in report.findings)


@requires_duckdb
class TestMatchReportAgeBreakdown:
    def test_age_brackets_populated_for_matched(self):
        # Use all 10 fixture rows — all should have known DOBs
        roster = [_voter(f"000000000{i}") for i in range(1, 10)] + [_voter("0000000010")]
        mapping = ColumnMapping(vuid="VUID", dob="DOB")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        # All 10 are in voterfile; by_age_bracket must have at least one key
        assert len(report.by_age_bracket) > 0
        # Total counts in by_age_bracket must equal matched_count
        assert sum(report.by_age_bracket.values()) == report.matched_count

    def test_known_age_bracket_vuid_001(self):
        # VUID 0000000001: DOB 19850315 → 41 on 2026-06-01 → "35-44"
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="VUID", dob="DOB")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert "35-44" in report.by_age_bracket
        assert report.by_age_bracket["35-44"] >= 1


@requires_duckdb
class TestMatchNoPiiInFindings:
    def test_no_vuid_in_finding_details(self):
        roster = [_voter("9999999999")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        for finding in report.findings:
            assert "9999999999" not in finding.detail, (
                f"VUID appears in finding detail: {finding.detail!r}"
            )

    def test_no_voter_name_in_finding_details(self):
        roster = [_voter("9999999999", voter_name="DOE, JOHN")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        for finding in report.findings:
            assert "DOE" not in finding.detail, (
                f"Voter name appears in finding detail: {finding.detail!r}"
            )
            assert "JOHN" not in finding.detail


@requires_duckdb
class TestCountyMismatchFinding:
    def test_county_mismatch_detected(self, tmp_path):
        """Roster record county='HARRIS' but voterfile row county='TRAVIS' → mismatch finding."""
        # VUID 0000000010 in the fixture has COUNTY=TRAVIS
        # Construct a roster record that says HARRIS for that VUID
        roster = [_voter("0000000010", county="HARRIS")]
        mapping = ColumnMapping(vuid="VUID", county="COUNTY")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        finding_types = [f.finding_type for f in report.findings]
        assert "county_mismatch" in finding_types

    def test_no_county_mismatch_when_counties_match(self):
        """VUID 0000000001 in fixture: COUNTY=HARRIS, roster county=HARRIS → no mismatch."""
        roster = [_voter("0000000001", county="HARRIS")]
        mapping = ColumnMapping(vuid="VUID", county="COUNTY")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        county_mismatches = [f for f in report.findings if f.finding_type == "county_mismatch"]
        assert len(county_mismatches) == 0


@requires_duckdb
class TestPrecinctMismatchFinding:
    def test_no_mismatch_when_roster_unpadded_and_voterfile_padded(self):
        """Civix-style precinct 510 vs voterfile 0510 for same VUID."""
        roster = [_voter("0000000001", precinct="510")]
        mapping = ColumnMapping(vuid="VUID", precinct="PCT")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        precinct_findings = [f for f in report.findings if f.finding_type == "precinct_mismatch"]
        assert len(precinct_findings) == 0

    def test_mismatch_when_precincts_truly_differ(self, tmp_path):
        roster = [_voter("0000000001", precinct="9999")]
        mapping = ColumnMapping(vuid="VUID", precinct="PCT")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert "precinct_mismatch" in [f.finding_type for f in report.findings]


@requires_duckdb
class TestDuplicateVoterfileVuids:
    def test_duplicate_vuid_rows_emit_finding(self, tmp_path):
        vf = tmp_path / "dup_vuid.csv"
        vf.write_text(
            '"VUID","PCT","COUNTY"\n'
            '"0000000001","0510","HARRIS"\n'
            '"0000000001","0512","HARRIS"\n',
            encoding="utf-8",
        )
        roster = [_voter("0000000001", precinct="510")]
        mapping = ColumnMapping(vuid="VUID", precinct="PCT", county="COUNTY")
        enriched, report = match_voterfile_to_roster(
            roster, vf, mapping, reference_date=_REF
        )
        assert enriched[0].in_voterfile is True
        assert enriched[0].vf_precinct == "0510"
        assert "duplicate_voterfile_vuids" in [f.finding_type for f in report.findings]


@requires_duckdb
class TestVoterfileRowCount:
    def test_total_voterfile_records_populated_when_enabled(self):
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster,
            SAMPLE_VOTERFILE,
            mapping,
            reference_date=_REF,
            count_voterfile=True,
        )
        assert report.total_voterfile_records == 10

    def test_total_voterfile_records_skipped_by_default(self):
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="VUID")
        _, report = match_voterfile_to_roster(
            roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert report.total_voterfile_records is None


@requires_duckdb
class TestMatchVoterfileEmptyRoster:
    def test_empty_roster_returns_empty_enriched(self):
        mapping = ColumnMapping(vuid="VUID")
        enriched, report = match_voterfile_to_roster(
            [], SAMPLE_VOTERFILE, mapping, reference_date=_REF
        )
        assert enriched == []
        assert report.total_roster_records == 0
        assert report.matched_count == 0


@requires_duckdb
class TestMatchVoterfileLongVuid:
    def test_eleven_digit_roster_matches_ten_digit_voterfile(self, tmp_path):
        vf = tmp_path / "long_vuid_vf.csv"
        vf.write_text(
            '"VUID","COUNTY"\n"2345678901","HARRIS"\n',
            encoding="utf-8",
        )
        roster = [_voter("12345678901")]
        mapping = ColumnMapping(vuid="VUID")
        enriched, report = match_voterfile_to_roster(
            roster, vf, mapping, reference_date=_REF
        )
        assert enriched[0].in_voterfile is True
        assert report.matched_count == 1


@requires_duckdb
class TestMappingConflictRaises:
    def test_duplicate_column_mapping_raises(self):
        roster = [_voter("0000000001")]
        mapping = ColumnMapping(vuid="VUID", county="VUID")
        with pytest.raises(ValueError, match="multiple standard fields"):
            match_voterfile_to_roster(
                roster, SAMPLE_VOTERFILE, mapping, reference_date=_REF
            )


# ---------------------------------------------------------------------------
# CSV round-trip
# ---------------------------------------------------------------------------


class TestWriteEnrichedCsvRoundtrip:
    def test_id_voter_preserved_with_leading_zeros(self, tmp_path):
        records = [
            _enriched("0000000001"),
            _enriched("0000000002"),
        ]
        out = tmp_path / "enriched.csv"
        write_enriched_csv(records, out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["ID_VOTER"] == "0000000001"
        assert rows[1]["ID_VOTER"] == "0000000002"

    def test_in_voterfile_lowercase_true(self, tmp_path):
        records = [_enriched("0000000001", in_voterfile=True)]
        out = tmp_path / "enriched.csv"
        write_enriched_csv(records, out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["IN_VOTERFILE"] == "true"

    def test_in_voterfile_lowercase_false(self, tmp_path):
        records = [_enriched("0000000001", in_voterfile=False)]
        out = tmp_path / "enriched.csv"
        write_enriched_csv(records, out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["IN_VOTERFILE"] == "false"

    def test_age_bracket_preserved(self, tmp_path):
        records = [_enriched("0000000001", age_bracket_val="35-44")]
        out = tmp_path / "enriched.csv"
        write_enriched_csv(records, out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["AGE_BRACKET"] == "35-44"

    def test_all_expected_columns_present(self, tmp_path):
        records = [_enriched("0000000001")]
        out = tmp_path / "enriched.csv"
        write_enriched_csv(records, out)
        with out.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or []
        expected = {
            "VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT", "COUNTY",
            "ELECTION_ID", "REPORT_DATE", "DUPLICATE_FLAG", "DUPLICATE_TYPE",
            "ALSO_FOUND_ON", "IN_VOTERFILE", "CD", "HD", "SD",
            "VF_COUNTY", "VF_PRECINCT", "AGE_BRACKET", "SEX", "HISPANIC", "VOTER_STATUS",
        }
        assert expected.issubset(set(fieldnames))

    def test_id_voter_is_string_type(self, tmp_path):
        records = [_enriched("0000000001")]
        out = tmp_path / "enriched.csv"
        write_enriched_csv(records, out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        # CSV always returns strings, but confirm the value matches expected format
        assert isinstance(rows[0]["ID_VOTER"], str)
        assert rows[0]["ID_VOTER"].startswith("0")  # leading zero preserved


# ---------------------------------------------------------------------------
# Mapping persistence
# ---------------------------------------------------------------------------


class TestSaveAndLoadMapping:
    def test_roundtrip(self, tmp_path):
        mapping = ColumnMapping(
            vuid="VUID",
            cd="CDPLANC2333",
            hd="HD2022",
            sd="SD2022",
        )
        path = tmp_path / "test.mapping.json"
        save_mapping(mapping, path)
        loaded = load_mapping(path)
        assert loaded.vuid == "VUID"
        assert loaded.cd == "CDPLANC2333"
        assert loaded.hd == "HD2022"
        assert loaded.sd == "SD2022"

    def test_sidecar_is_valid_json(self, tmp_path):
        mapping = ColumnMapping(vuid="VUID", county="COUNTY")
        path = tmp_path / "test.mapping.json"
        save_mapping(mapping, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["vuid"] == "VUID"
        assert data["county"] == "COUNTY"

    def test_none_fields_preserved(self, tmp_path):
        mapping = ColumnMapping(vuid="VUID")  # all others are None
        path = tmp_path / "test.mapping.json"
        save_mapping(mapping, path)
        loaded = load_mapping(path)
        assert loaded.cd is None
        assert loaded.hd is None
        assert loaded.dob is None

    def test_metadata_fields_preserved(self, tmp_path):
        mapping = ColumnMapping(
            vuid="VUID",
            voterfile_path="/path/to/voterfile.csv",
            created_at="2026-05-24T00:00:00",
        )
        path = tmp_path / "test.mapping.json"
        save_mapping(mapping, path)
        loaded = load_mapping(path)
        assert loaded.voterfile_path == "/path/to/voterfile.csv"
        assert loaded.created_at == "2026-05-24T00:00:00"

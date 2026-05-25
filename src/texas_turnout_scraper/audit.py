"""Data quality audit for texas-turnout-scraper.

This module runs post-processing audits on already-fetched roster data.
It never fetches live data and never logs PII (VUID values or voter names).

Usage:
    from texas_turnout_scraper.audit import audit_roster, audit_from_csv
"""

from __future__ import annotations

import collections
import csv
from datetime import date, datetime
from pathlib import Path

from .enums import VoteMethod
from .models import AuditFinding, AuditReport, CountyRoster, CountyTurnout, VoterRecord


def audit_roster(
    rosters: list[CountyRoster],
    turnout_summary: list[CountyTurnout] | None = None,
    election_id: str | None = None,
    report_date: date | None = None,
    source: str = "unknown",
) -> AuditReport:
    """Run all audit checks on a list of county rosters.

    Checks:
    1. Duplicate VUIDs within a single county
    2. Duplicate VUIDs across counties (same VUID appearing in multiple counties)
    3. Cross-method duplicates: same VUID with both IN-PERSON and MAIL-IN
    4. Turnout anomaly: county roster count > county registered_voters (if turnout_summary provided)
    5. Missing counties: counties in turnout_summary absent from rosters (if provided)

    Args:
        rosters: List of CountyRoster objects to audit.
        turnout_summary: Optional list of CountyTurnout records to cross-check against.
        election_id: Override election_id (defaults to first roster's election_id).
        report_date: Override report_date (defaults to first roster's report_date).
        source: Source label for the AuditReport.

    Returns:
        AuditReport with all findings populated.
    """
    findings: list[AuditFinding] = []

    # Derive election_id and report_date from rosters if not explicitly provided
    _election_id = election_id or (rosters[0].election_id if rosters else "unknown")
    _report_date = report_date or (rosters[0].report_date if rosters else date.today())

    # ------------------------------------------------------------------
    # Build data structures for analysis
    # ------------------------------------------------------------------

    # Global VUID counter across all rosters
    global_vuid_counter: collections.Counter[str] = collections.Counter()

    # Per-VUID set of VoteMethods (for cross-method duplicate detection)
    vuid_methods: dict[str, set[str]] = collections.defaultdict(set)

    # Per-VUID set of counties (for cross-county duplicate detection)
    vuid_counties: dict[str, set[str]] = collections.defaultdict(set)

    total_records = 0

    for county_roster in rosters:
        county_name = county_roster.county

        # Per-county VUID counter (for within-county duplicates)
        county_vuid_counter: collections.Counter[str] = collections.Counter()

        for record in county_roster.records:
            vid = record.id_voter
            county_vuid_counter[vid] += 1
            global_vuid_counter[vid] += 1
            vuid_methods[vid].add(record.voting_method)
            vuid_counties[vid].add(county_name)
            total_records += 1

        # Check 1: Duplicate VUIDs within this county
        within_county_dups = {
            vid: count for vid, count in county_vuid_counter.items() if count > 1
        }
        if within_county_dups:
            dup_count = len(within_county_dups)
            total_dup_records = sum(within_county_dups.values())
            findings.append(
                AuditFinding(
                    finding_type="duplicate_vuid",
                    county=county_name,
                    detail=(
                        f"{dup_count} VUID(s) appear more than once within {county_name} "
                        f"({total_dup_records} affected records)"
                    ),
                    severity="error",
                )
            )

    # ------------------------------------------------------------------
    # Check 2: Duplicate VUIDs across counties (cross-county)
    # ------------------------------------------------------------------
    cross_county_dups = {
        vid: counties
        for vid, counties in vuid_counties.items()
        if len(counties) > 1
    }
    if cross_county_dups:
        # Group by the set of counties for a compact summary
        cross_county_count = len(cross_county_dups)
        findings.append(
            AuditFinding(
                finding_type="duplicate_vuid",
                county=None,
                detail=(
                    f"{cross_county_count} VUID(s) appear in more than one county "
                    f"(cross-county duplicates)"
                ),
                severity="error",
            )
        )

    # ------------------------------------------------------------------
    # Check 3: Cross-method duplicates (IN-PERSON and MAIL-IN for same VUID)
    # ------------------------------------------------------------------
    cross_method_vuids = {
        vid: methods
        for vid, methods in vuid_methods.items()
        if len(methods) > 1
    }
    cross_method_count = len(cross_method_vuids)
    if cross_method_vuids:
        # Identify which counties are involved (without logging VUIDs)
        affected_counties: set[str] = set()
        for vid in cross_method_vuids:
            affected_counties.update(vuid_counties[vid])
        findings.append(
            AuditFinding(
                finding_type="cross_method_duplicate",
                county=None,
                detail=(
                    f"{cross_method_count} VUID(s) appear with both IN-PERSON and MAIL-IN "
                    f"voting methods; affected counties: {', '.join(sorted(affected_counties))}"
                ),
                severity="error",
            )
        )

    # ------------------------------------------------------------------
    # Check 4 & 5: Turnout-based checks (require turnout_summary)
    # ------------------------------------------------------------------
    if turnout_summary is not None:
        # Build a lookup from county name → CountyRoster for fast access
        roster_by_county: dict[str, CountyRoster] = {
            r.county.upper(): r for r in rosters
        }

        for turnout in turnout_summary:
            county_key = turnout.county.upper()
            matching_roster = roster_by_county.get(county_key)

            if matching_roster is None:
                # Check 5: Missing county
                findings.append(
                    AuditFinding(
                        finding_type="missing_county",
                        county=turnout.county,
                        detail=(
                            f"County '{turnout.county}' is present in turnout summary "
                            f"but has no corresponding roster"
                        ),
                        severity="warning",
                    )
                )
            else:
                # Check 4: Turnout anomaly — roster record count exceeds registered voters
                roster_count = len(matching_roster.records)
                registered = turnout.registered_voters
                if registered > 0 and roster_count > registered:
                    findings.append(
                        AuditFinding(
                            finding_type="turnout_anomaly",
                            county=turnout.county,
                            detail=(
                                f"County '{turnout.county}' has {roster_count} roster records "
                                f"but only {registered} registered voters — "
                                f"excess: {roster_count - registered}"
                            ),
                            severity="warning",
                        )
                    )

    # ------------------------------------------------------------------
    # Aggregate metrics
    # ------------------------------------------------------------------
    unique_vuids = len(global_vuid_counter)
    duplicate_vuid_count = sum(
        1 for count in global_vuid_counter.values() if count > 1
    )

    return AuditReport(
        election_id=_election_id,
        report_date=_report_date,
        source=source,
        total_records=total_records,
        unique_vuids=unique_vuids,
        duplicate_vuid_count=duplicate_vuid_count,
        cross_method_duplicate_count=cross_method_count,
        findings=findings,
        generated_at=datetime.utcnow(),
    )


def audit_from_csv(
    csv_path: Path,
    election_id: str,
    report_date: date,
    source: str = "unknown",
) -> AuditReport:
    """Load a single roster CSV file and run audit inline.

    CSV format expected: VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT
    The VOTER_NAME column is read but immediately discarded (PII).
    County name is extracted from the filename if possible, otherwise
    falls back to the stem of the file.

    Args:
        csv_path: Path to the roster CSV file.
        election_id: Election identifier (source_election_id string).
        report_date: The report date this CSV corresponds to.
        source: Source label carried through to the AuditReport.

    Returns:
        AuditReport produced by running audit_roster on the loaded data.
    """
    csv_path = Path(csv_path)

    # Attempt to extract county from filename, e.g. "roster_HARRIS_2024-10-21.csv"
    stem = csv_path.stem  # e.g. "roster_HARRIS_2024-10-21" or "roster_2024-10-21"
    parts = stem.split("_")
    # Heuristic: if there are at least 3 parts and the middle part(s) are not a date,
    # treat them as the county name.
    county = "UNKNOWN"
    if len(parts) >= 3:
        # parts[0] is likely "roster", parts[-1] and parts[-2] likely date fragments
        # Try to identify the county segment(s) between prefix and date
        candidate_parts: list[str] = []
        for part in parts[1:]:
            # A date fragment looks like 4 digits or matches YYYY/MM/DD patterns
            if part.isdigit() and len(part) == 4:
                break
            if len(part) == 2 and part.isdigit():
                break
            candidate_parts.append(part)
        if candidate_parts:
            county = "_".join(candidate_parts)

    records: list[VoterRecord] = []

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_id = row.get("ID_VOTER", "").strip()
            raw_method = row.get("VOTING_METHOD", "").strip().upper()
            raw_precinct = row.get("PRECINCT", "").strip()

            if not raw_id:
                continue  # skip rows with no VUID

            try:
                vote_method = VoteMethod(raw_method)
            except ValueError:
                # Default to IN_PERSON if method is unrecognised
                vote_method = VoteMethod.IN_PERSON

            # VOTER_NAME is read by DictReader but never stored or logged
            row_county = row.get("COUNTY", county).strip() or county
            row_election_id = str(row.get("ELECTION_ID") or election_id)
            row_report_date = report_date
            raw_report_date = row.get("REPORT_DATE", "").strip()
            if raw_report_date:
                from datetime import datetime

                row_report_date = datetime.strptime(raw_report_date, "%Y-%m-%d").date()

            records.append(
                VoterRecord(
                    id_voter=raw_id,
                    voting_method=vote_method,
                    precinct=raw_precinct,
                    county=row_county,
                    election_id=row_election_id,
                    report_date=row_report_date,
                )
            )

    roster = CountyRoster(
        county=county,
        election_id=election_id,
        report_date=report_date,
        source=source,
        records=records,
    )

    return audit_roster(
        rosters=[roster],
        election_id=election_id,
        report_date=report_date,
        source=source,
    )

"""Data quality audit for texas-turnout-scraper.

Canonical entry point: :func:`audit_records`. Never logs PII (VUIDs or voter names).
"""

from __future__ import annotations

import collections
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

from .enums import FindingType, VoteMethod
from .models import AuditFinding, AuditReport, CountyRoster, CountyTurnout, VoterRecord


def _vuid_index(
    records: list[VoterRecord],
) -> tuple[
    dict[str, set[date]],
    dict[str, set[str]],
    dict[str, set[VoteMethod]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    vuid_dates: dict[str, set[date]] = collections.defaultdict(set)
    vuid_counties: dict[str, set[str]] = collections.defaultdict(set)
    vuid_methods: dict[str, set[VoteMethod]] = collections.defaultdict(set)
    vuid_names: dict[str, set[str]] = collections.defaultdict(set)
    vuid_precincts: dict[str, set[str]] = collections.defaultdict(set)
    for rec in records:
        vuid = rec.id_voter
        vuid_dates[vuid].add(rec.report_date)
        vuid_counties[vuid].add(rec.county)
        vuid_methods[vuid].add(rec.voting_method)
        vuid_names[vuid].add(rec.voter_name)
        vuid_precincts[vuid].add(rec.precinct)
    return vuid_dates, vuid_counties, vuid_methods, vuid_names, vuid_precincts


def _check_multiple_dates(records: list[VoterRecord]) -> list[AuditFinding]:
    if not records:
        return []
    vuid_dates, _, _, _, _ = _vuid_index(records)
    affected = sum(1 for dates in vuid_dates.values() if len(dates) > 1)
    if not affected:
        return []
    return [
        AuditFinding(
            finding_type=FindingType.MULTIPLE_DATES.value,
            severity="error",
            detail=(
                f"{affected} VUID(s) appear on more than one report date "
                f"({len(records)} total records)"
            ),
        )
    ]


def _check_duplicate_vuids(records: list[VoterRecord]) -> list[AuditFinding]:
    if not records:
        return []
    vuid_row_counts = collections.Counter(r.id_voter for r in records)
    affected = sum(1 for count in vuid_row_counts.values() if count > 1)
    if not affected:
        return []
    return [
        AuditFinding(
            finding_type=FindingType.DUPLICATE_VUID.value,
            severity="warning",
            detail=(
                f"{affected} VUID(s) appear more than once in the roster "
                f"({len(records)} total records)"
            ),
        )
    ]


def _check_conflicting_methods(records: list[VoterRecord]) -> list[AuditFinding]:
    if not records:
        return []
    _, _, vuid_methods, _, _ = _vuid_index(records)
    affected = {vid for vid, methods in vuid_methods.items() if len(methods) > 1}
    if not affected:
        return []
    return [
        AuditFinding(
            finding_type=FindingType.CONFLICTING_METHOD.value,
            severity="error",
            detail=(
                f"{len(affected)} VUID(s) appear with both IN-PERSON and MAIL-IN "
                f"voting methods"
            ),
        )
    ]


def _check_multiple_counties(records: list[VoterRecord]) -> list[AuditFinding]:
    if not records:
        return []
    _, vuid_counties, _, _, _ = _vuid_index(records)
    affected = {vid for vid, counties in vuid_counties.items() if len(counties) > 1}
    if not affected:
        return []
    return [
        AuditFinding(
            finding_type=FindingType.MULTIPLE_COUNTIES.value,
            severity="error",
            detail=(
                f"{len(affected)} VUID(s) appear in more than one county "
                f"(cross-county duplicates)"
            ),
        )
    ]


def _check_name_mismatches(records: list[VoterRecord]) -> list[AuditFinding]:
    if not records:
        return []
    _, _, _, vuid_names, _ = _vuid_index(records)
    affected = {vid for vid, names in vuid_names.items() if len(names) > 1}
    if not affected:
        return []
    return [
        AuditFinding(
            finding_type=FindingType.NAME_MISMATCH.value,
            severity="warning",
            detail=(
                f"{len(affected)} VUID(s) have differing voter names across appearances"
            ),
        )
    ]


def _check_precinct_mismatches(records: list[VoterRecord]) -> list[AuditFinding]:
    if not records:
        return []
    _, _, _, _, vuid_precincts = _vuid_index(records)
    affected = {vid for vid, precincts in vuid_precincts.items() if len(precincts) > 1}
    if not affected:
        return []
    return [
        AuditFinding(
            finding_type=FindingType.PRECINCT_MISMATCH.value,
            severity="warning",
            detail=(
                f"{len(affected)} VUID(s) have differing precincts across appearances"
            ),
        )
    ]


def _check_turnout_anomalies(
    records: list[VoterRecord],
    turnout: list[CountyTurnout],
) -> list[AuditFinding]:
    roster_by_county = collections.defaultdict(int)
    for rec in records:
        roster_by_county[rec.county.upper()] += 1

    findings: list[AuditFinding] = []
    for row in turnout:
        county_key = row.county.upper()
        roster_count = roster_by_county.get(county_key, 0)
        registered = row.registered_voters
        if registered > 0 and roster_count > registered:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.TURNOUT_ANOMALY.value,
                    county=row.county,
                    severity="warning",
                    detail=(
                        f"County '{row.county}' has {roster_count} roster records "
                        f"but only {registered} registered voters — "
                        f"excess: {roster_count - registered}"
                    ),
                )
            )
    return findings


def _check_missing_counties(
    records: list[VoterRecord],
    turnout: list[CountyTurnout],
) -> list[AuditFinding]:
    roster_counties = {rec.county.upper() for rec in records}
    findings: list[AuditFinding] = []
    for row in turnout:
        if row.county.upper() not in roster_counties:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.MISSING_COUNTY.value,
                    county=row.county,
                    severity="warning",
                    detail=(
                        f"County '{row.county}' is present in turnout summary "
                        f"but has no corresponding roster"
                    ),
                )
            )
    return findings


def audit_records(
    records: Iterable[VoterRecord],
    *,
    turnout: list[CountyTurnout] | None = None,
    election_id: str | None = None,
    report_date: date | None = None,
    source: str = "unknown",
) -> AuditReport:
    """Run all audit checks on a flat roster record list."""
    materialized = list(records)
    findings: list[AuditFinding] = []
    findings.extend(_check_multiple_dates(materialized))
    findings.extend(_check_duplicate_vuids(materialized))
    findings.extend(_check_conflicting_methods(materialized))
    findings.extend(_check_multiple_counties(materialized))
    findings.extend(_check_name_mismatches(materialized))
    findings.extend(_check_precinct_mismatches(materialized))

    if turnout is not None:
        findings.extend(_check_turnout_anomalies(materialized, turnout))
        findings.extend(_check_missing_counties(materialized, turnout))

    _, _, vuid_methods, _, _ = _vuid_index(materialized)
    cross_method_count = sum(1 for methods in vuid_methods.values() if len(methods) > 1)
    vuid_row_counts = collections.Counter(r.id_voter for r in materialized)
    duplicate_vuid_count = sum(1 for count in vuid_row_counts.values() if count > 1)

    eid = election_id or (materialized[0].election_id if materialized else "unknown")
    rdate = report_date or (
        materialized[0].report_date if materialized else date.today()
    )

    return AuditReport(
        election_id=eid,
        report_date=rdate,
        source=source,
        total_records=len(materialized),
        unique_vuids=len(vuid_row_counts),
        duplicate_vuid_count=duplicate_vuid_count,
        cross_method_duplicate_count=cross_method_count,
        findings=findings,
        generated_at=datetime.now(timezone.utc),
        audit_schema_version="2.0",
    )


def audit_roster(
    rosters: list[CountyRoster],
    turnout_summary: list[CountyTurnout] | None = None,
    election_id: str | None = None,
    report_date: date | None = None,
    source: str = "unknown",
) -> AuditReport:
    """Run audit checks on county rosters (convenience wrapper)."""
    records = [rec for roster in rosters for rec in roster.records]
    return audit_records(
        records,
        turnout=turnout_summary,
        election_id=election_id,
        report_date=report_date,
        source=source,
    )


def audit_from_csv(
    csv_path: Path,
    election_id: str,
    report_date: date,
    source: str = "unknown",
) -> AuditReport:
    """Load a roster CSV and run :func:`audit_records`."""
    from .writer import read_roster_csv

    records = read_roster_csv(Path(csv_path))
    return audit_records(
        records,
        election_id=election_id,
        report_date=report_date,
        source=source,
    )

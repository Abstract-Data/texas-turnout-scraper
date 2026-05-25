"""Roster accumulation, duplicate detection, and CSV writer.

After all EV dates are fetched (as CountyRoster objects), call ``accumulate_roster()``
to merge them into a single flagged list of VoterRecords — one file per election.

Duplicate detection runs in a single pass over all records and flags five conditions:

    multiple_dates      — same VUID appears on more than one report date
    conflicting_method  — same VUID recorded with both IN-PERSON and MAIL-IN
    multiple_counties   — same VUID appears in more than one county
    name_mismatch       — same VUID but VOTER_NAME differs across appearances
    precinct_mismatch   — same VUID but PRECINCT differs across appearances

All five conditions are independent — a single VUID may be flagged for several at once.
``duplicate_type`` is a comma-separated string of the applicable flags.
``also_found_on`` is a semicolon-separated string of "COUNTY|YYYY-MM-DD" pairs
for every OTHER appearance of the same VUID (excluding the current row).

PII note: VOTER_NAME is used internally for name_mismatch detection only.
It is written to the CSV output file (SOS public record) but MUST NOT be logged,
included in MCP responses, or surfaced in exception messages.
"""

from __future__ import annotations

import collections
import csv
import io
from datetime import date
from pathlib import Path

from .enums import VoteMethod
from .models import AuditFinding, AuditReport, CountyRoster, VoterRecord


def _duplicate_flags(duplicate_type: str) -> set[str]:
    """Parse comma-separated duplicate_type flags (exact tokens only)."""
    if not duplicate_type:
        return set()
    return {part.strip() for part in duplicate_type.split(",") if part.strip()}


def _has_duplicate_flag(duplicate_type: str, flag: str) -> bool:
    return flag in _duplicate_flags(duplicate_type)


def _appearance_token(rec: VoterRecord) -> str:
    return f"{rec.county}|{rec.report_date.isoformat()}"


def _dedupe_tokens_preserve_order(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


def accumulate_roster(rosters: list[CountyRoster]) -> list[VoterRecord]:
    """Merge county rosters from all EV dates and flag duplicate conditions.

    Runs a single-pass analysis over all records across all rosters.
    Returns a new list of VoterRecord objects with duplicate fields populated.
    Input records are not mutated.

    Duplicate conditions detected (set in ``duplicate_type``, comma-separated):
    - ``multiple_dates``     — VUID appears on more than one ``report_date``
    - ``conflicting_method`` — VUID recorded with both IN-PERSON and MAIL-IN
    - ``multiple_counties``  — VUID appears in more than one county
    - ``name_mismatch``      — VUID has differing VOTER_NAME across appearances
    - ``precinct_mismatch``  — VUID has differing PRECINCT across appearances

    ``also_found_on`` is set to a semicolon-separated string of
    ``"COUNTY|YYYY-MM-DD"`` tokens for every OTHER appearance of the same VUID.

    Args:
        rosters: List of per-county, per-date rosters to merge.

    Returns:
        Flat list of VoterRecord with duplicate fields populated.
        One row per original appearance — duplicates flagged, not collapsed.
    """
    # Flatten all records, preserving original order
    all_records: list[VoterRecord] = []
    for roster in rosters:
        all_records.extend(roster.records)

    if not all_records:
        return []

    # --- Build a per-VUID index of all appearances -------------------------
    # appearance: (county, report_date) tuple
    vuid_dates: dict[str, set[date]] = collections.defaultdict(set)
    vuid_counties: dict[str, set[str]] = collections.defaultdict(set)
    vuid_methods: dict[str, set[VoteMethod]] = collections.defaultdict(set)
    vuid_names: dict[str, set[str]] = collections.defaultdict(set)
    vuid_precincts: dict[str, set[str]] = collections.defaultdict(set)

    appearance_tokens = [_appearance_token(rec) for rec in all_records]
    vuid_row_indices: dict[str, list[int]] = collections.defaultdict(list)

    for i, rec in enumerate(all_records):
        vuid = rec.id_voter
        vuid_dates[vuid].add(rec.report_date)
        vuid_counties[vuid].add(rec.county)
        vuid_methods[vuid].add(rec.voting_method)
        vuid_names[vuid].add(rec.voter_name)
        vuid_precincts[vuid].add(rec.precinct)
        vuid_row_indices[vuid].append(i)

    # --- Build flagged records ---------------------------------------------
    flagged: list[VoterRecord] = []
    for i, rec in enumerate(all_records):
        vuid = rec.id_voter

        flags: list[str] = []

        if len(vuid_dates[vuid]) > 1:
            flags.append("multiple_dates")

        if len(vuid_methods[vuid]) > 1:
            flags.append("conflicting_method")

        if len(vuid_counties[vuid]) > 1:
            flags.append("multiple_counties")

        if len(vuid_names[vuid]) > 1:
            flags.append("name_mismatch")

        if len(vuid_precincts[vuid]) > 1:
            flags.append("precinct_mismatch")

        # Tokens from every other row with the same VUID (index-based, not token-based)
        other_tokens = [
            appearance_tokens[j]
            for j in vuid_row_indices[vuid]
            if j != i
        ]
        unique_others = _dedupe_tokens_preserve_order(other_tokens)

        flagged.append(
            VoterRecord(
                id_voter=rec.id_voter,
                voting_method=rec.voting_method,
                precinct=rec.precinct,
                county=rec.county,
                election_id=rec.election_id,
                report_date=rec.report_date,
                voter_name=rec.voter_name,
                duplicate_flag=bool(flags),
                duplicate_type=",".join(flags),
                also_found_on="; ".join(unique_others),
            )
        )

    return flagged


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

# Columns written to the election roster CSV file.
# voter_name is included (SOS public record) but excluded from MCP/API responses.
ROSTER_CSV_COLUMNS = [
    "VOTER_NAME",
    "ID_VOTER",
    "VOTING_METHOD",
    "PRECINCT",
    "COUNTY",
    "ELECTION_ID",
    "REPORT_DATE",
    "DUPLICATE_FLAG",
    "DUPLICATE_TYPE",
    "ALSO_FOUND_ON",
]
_ROSTER_COLUMNS = ROSTER_CSV_COLUMNS


def write_roster_csv(records: list[VoterRecord], path: Path) -> Path:
    """Write a flat accumulated roster to a CSV file.

    Creates parent directories as needed. Overwrites existing file.

    Args:
        records: Flagged VoterRecord list from ``accumulate_roster()``.
        path: Destination path for the CSV file.

    Returns:
        The path written to.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_ROSTER_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for rec in records:
            writer.writerow(
                {
                    "VOTER_NAME": rec.voter_name,
                    "ID_VOTER": rec.id_voter,
                    "VOTING_METHOD": rec.voting_method.value,
                    "PRECINCT": rec.precinct,
                    "COUNTY": rec.county,
                    "ELECTION_ID": rec.election_id,
                    "REPORT_DATE": rec.report_date.isoformat(),
                    "DUPLICATE_FLAG": str(rec.duplicate_flag).lower(),
                    "DUPLICATE_TYPE": rec.duplicate_type,
                    "ALSO_FOUND_ON": rec.also_found_on,
                }
            )
    return path


def roster_csv_to_text(records: list[VoterRecord]) -> str:
    """Serialize a roster to CSV text (no file I/O) — useful for testing."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_ROSTER_COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for rec in records:
        writer.writerow(
            {
                "VOTER_NAME": rec.voter_name,
                "ID_VOTER": rec.id_voter,
                "VOTING_METHOD": rec.voting_method.value,
                "PRECINCT": rec.precinct,
                "COUNTY": rec.county,
                "ELECTION_ID": rec.election_id,
                "REPORT_DATE": rec.report_date.isoformat(),
                "DUPLICATE_FLAG": str(rec.duplicate_flag).lower(),
                "DUPLICATE_TYPE": rec.duplicate_type,
                "ALSO_FOUND_ON": rec.also_found_on,
            }
        )
    return buf.getvalue()


def read_roster_csv(path: Path) -> list[VoterRecord]:
    """Read a previously written roster CSV back into VoterRecord objects.

    PII note: VOTER_NAME is read but immediately placed into VoterRecord.voter_name.
    It is never logged.

    Args:
        path: Path to a roster CSV written by ``write_roster_csv()``.

    Returns:
        List of VoterRecord. Duplicate fields are preserved from the file.
    """
    from datetime import datetime  # local import to keep module-level imports light

    records: list[VoterRecord] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("Roster CSV is missing a header row")
        missing = [col for col in _ROSTER_COLUMNS if col not in reader.fieldnames]
        if missing:
            raise ValueError(f"Roster CSV missing required columns: {', '.join(missing)}")
        for row in reader:
            method_raw = row["VOTING_METHOD"]
            try:
                voting_method = VoteMethod(method_raw)
            except ValueError as exc:
                raise ValueError(f"Invalid VOTING_METHOD value in roster CSV: {method_raw!r}") from exc
            records.append(
                VoterRecord(
                    voter_name=row["VOTER_NAME"],  # PII — do not log
                    id_voter=str(row["ID_VOTER"]).zfill(10),  # always str — never int
                    voting_method=voting_method,
                    precinct=row["PRECINCT"],
                    county=row["COUNTY"],
                    election_id=str(row["ELECTION_ID"]),
                    report_date=datetime.strptime(row["REPORT_DATE"], "%Y-%m-%d").date(),
                    duplicate_flag=row["DUPLICATE_FLAG"].lower() == "true",
                    duplicate_type=row["DUPLICATE_TYPE"],
                    also_found_on=row["ALSO_FOUND_ON"],
                )
            )
    return records


# ---------------------------------------------------------------------------
# AuditReport from accumulated records
# ---------------------------------------------------------------------------


def audit_from_records(
    records: list[VoterRecord],
    election_id: str | None = None,
    report_date: date | None = None,
    source: str = "unknown",
) -> AuditReport:
    """Build an AuditReport from an already-accumulated (flagged) record list.

    Counts each duplicate type. Does NOT re-run detection — reads the flags
    already set by ``accumulate_roster()``.

    PII note: finding details contain only counts and county names — never VUIDs or names.
    """
    total = len(records)
    unique_vuids = len({r.id_voter for r in records})

    multiple_dates = [r for r in records if _has_duplicate_flag(r.duplicate_type, "multiple_dates")]
    conflicting = [r for r in records if _has_duplicate_flag(r.duplicate_type, "conflicting_method")]
    multi_county = [r for r in records if _has_duplicate_flag(r.duplicate_type, "multiple_counties")]
    name_mismatch = [r for r in records if _has_duplicate_flag(r.duplicate_type, "name_mismatch")]
    precinct_mismatch = [r for r in records if _has_duplicate_flag(r.duplicate_type, "precinct_mismatch")]

    findings: list[AuditFinding] = []

    if multiple_dates:
        findings.append(AuditFinding(
            finding_type="multiple_dates",
            severity="error",
            detail=f"{len(multiple_dates)} appearances where same VUID found on multiple report dates",
        ))

    if conflicting:
        findings.append(AuditFinding(
            finding_type="conflicting_method",
            severity="error",
            detail=f"{len(conflicting)} appearances where same VUID has both IN-PERSON and MAIL-IN",
        ))

    if multi_county:
        counties_affected = {r.county for r in multi_county}
        findings.append(AuditFinding(
            finding_type="multiple_counties",
            severity="error",
            detail=f"{len(multi_county)} appearances where same VUID found in multiple counties "
                   f"({len(counties_affected)} counties affected)",
        ))

    if name_mismatch:
        findings.append(AuditFinding(
            finding_type="name_mismatch",
            severity="warning",
            detail=f"{len(name_mismatch)} appearances where same VUID has differing voter names",
        ))

    if precinct_mismatch:
        findings.append(AuditFinding(
            finding_type="precinct_mismatch",
            severity="warning",
            detail=f"{len(precinct_mismatch)} appearances where same VUID has differing precincts",
        ))

    eid = election_id or (records[0].election_id if records else "unknown")
    rdate = report_date or (records[0].report_date if records else date.today())

    return AuditReport(
        election_id=eid,
        report_date=rdate,
        source=source,
        total_records=total,
        unique_vuids=unique_vuids,
        duplicate_vuid_count=len({r.id_voter for r in records if r.duplicate_flag}),
        cross_method_duplicate_count=len({r.id_voter for r in conflicting}),
        findings=findings,
    )


def stored_roster_ev_path(data_dir: Path, source: str, election_id: str) -> Path:
    """Path to the combined per-election EV roster CSV on disk."""
    return data_dir / "elections" / source / election_id / f"roster_ev_{election_id}.csv"


def stored_audit_ev_path(data_dir: Path, source: str, election_id: str) -> Path:
    """Path to the combined per-election EV audit JSON on disk."""
    return data_dir / "elections" / source / election_id / f"audit_ev_{election_id}.json"


def report_date_from_roster_csv(csv_path: Path) -> date:
    """Return the latest report_date present in a combined roster CSV."""
    records = read_roster_csv(csv_path)
    if not records:
        return date.today()
    return max(r.report_date for r in records)

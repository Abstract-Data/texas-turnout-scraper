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
from .models import CountyRoster, CountyTurnout, VoterRecord


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
        other_tokens = [appearance_tokens[j] for j in vuid_row_indices[vuid] if j != i]
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
                VoteMethod(method_raw)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid VOTING_METHOD value in roster CSV: {method_raw!r}"
                ) from exc
            report_date = datetime.strptime(row["REPORT_DATE"], "%Y-%m-%d").date()
            rec = VoterRecord.from_csv_row(
                row,
                county=row["COUNTY"],
                election_id=str(row["ELECTION_ID"]),
                report_date=report_date,
            )
            records.append(
                rec.model_copy(
                    update={
                        "duplicate_flag": row["DUPLICATE_FLAG"].lower() == "true",
                        "duplicate_type": row["DUPLICATE_TYPE"],
                        "also_found_on": row["ALSO_FOUND_ON"],
                    }
                )
            )
    return records


def stored_roster_ev_path(data_dir: Path, source: str, election_id: str) -> Path:
    """Path to the combined per-election EV roster CSV on disk."""
    return data_dir / "elections" / source / election_id / f"roster_ev_{election_id}.csv"


def stored_audit_ev_path(data_dir: Path, source: str, election_id: str) -> Path:
    """Path to the combined per-election EV audit JSON on disk."""
    return data_dir / "elections" / source / election_id / f"audit_ev_{election_id}.json"


def stored_ed_turnout_path(output_dir: Path, election_id: str, election_date: date) -> Path:
    """Path to the per-election Election Day turnout CSV.

    The ED file mirrors the EV per-date turnout file naming convention
    (``turnout_ed_{YYYY-MM-DD}.csv``) so the audit loader can find it
    with the same per-source glob.
    """
    return output_dir / election_id / f"turnout_ed_{election_date.isoformat()}.csv"


def stored_roster_ed_path(output_dir: Path, election_id: str, election_date: date) -> Path:
    """Path to the election-day voter roster CSV parsed from the statewide ZIP."""
    return output_dir / election_id / f"roster_ed_{election_date.isoformat()}.csv"


def stored_statewide_ed_zip_path(
    output_dir: Path,
    election_id: str,
    election_date: date,
) -> Path:
    """Path to the raw election-day statewide report ZIP from Civix."""
    return (
        output_dir
        / election_id
        / f"statewide_ed_{election_id}_{election_date.isoformat()}.zip"
    )


def report_date_from_roster_csv(csv_path: Path) -> date:
    """Return the latest report_date present in a combined roster CSV."""
    records = read_roster_csv(csv_path)
    if not records:
        return date.today()
    return max(r.report_date for r in records)


_TURNOUT_CSV_COLUMNS = (
    "election_id",
    "report_date",
    "county",
    "county_id",
    "registered_voters",
    "in_person_votes_on_date",
    "total_in_person_votes",
    "total_mail_votes",
    "roster_available",
    "source",
)


def _turnout_glob_patterns(source: str) -> tuple[str, ...]:
    """Glob patterns that match per-date turnout CSV files for a source.

    Civix tracks two stages — early voting (``turnout_ev_*.csv``) and
    election day (``turnout_ed_*.csv``). Legacy only writes the unprefixed
    ``turnout_*.csv`` form.
    """
    if source == "civix":
        return ("turnout_ev_*.csv", "turnout_ed_*.csv")
    return ("turnout_*.csv",)


def _date_from_turnout_filename(path: Path, source: str) -> date | None:
    stem = path.stem
    if source == "civix":
        for prefix in ("turnout_ev_", "turnout_ed_"):
            if stem.startswith(prefix):
                try:
                    return date.fromisoformat(stem.removeprefix(prefix))
                except ValueError:
                    return None
        return None
    if source == "legacy" and stem.startswith("turnout_"):
        try:
            return date.fromisoformat(stem.removeprefix("turnout_"))
        except ValueError:
            return None
    return None


def stored_turnout_paths(data_dir: Path, source: str, election_id: str) -> list[Path]:
    """Sorted paths to per-date turnout CSV files for an election, if any exist.

    For Civix, this includes both early-voting (``turnout_ev_*.csv``) and
    election-day (``turnout_ed_*.csv``) files. For legacy, only the
    unprefixed ``turnout_*.csv`` form is matched.
    """
    election_dir = data_dir / "elections" / source / election_id
    if not election_dir.is_dir():
        return []
    matches: set[Path] = set()
    for pattern in _turnout_glob_patterns(source):
        matches.update(election_dir.glob(pattern))
    return sorted(matches)


def read_turnout_csv(path: Path) -> list[CountyTurnout]:
    """Read a stored county turnout CSV into :class:`CountyTurnout` rows."""
    from datetime import datetime

    rows: list[CountyTurnout] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("Turnout CSV is missing a header row")
        missing = [col for col in _TURNOUT_CSV_COLUMNS if col not in reader.fieldnames]
        if missing:
            raise ValueError(f"Turnout CSV missing required columns: {', '.join(missing)}")
        for row in reader:
            county_id_raw = row.get("county_id", "").strip()
            county_id = int(county_id_raw) if county_id_raw else None
            roster_raw = row.get("roster_available", "").strip().lower()
            rows.append(
                CountyTurnout(
                    election_id=str(row["election_id"]),
                    report_date=datetime.strptime(row["report_date"], "%Y-%m-%d").date(),
                    county=row["county"],
                    county_id=county_id,
                    registered_voters=int(row["registered_voters"]),
                    in_person_votes_on_date=int(row["in_person_votes_on_date"]),
                    total_in_person_votes=int(row["total_in_person_votes"]),
                    total_mail_votes=int(row["total_mail_votes"]),
                    roster_available=roster_raw in {"true", "1", "yes"},
                    source=row["source"],
                )
            )
    return rows


def load_stored_turnout_for_audit(
    data_dir: Path,
    source: str,
    election_id: str,
    *,
    report_dates: set[date] | None = None,
) -> list[CountyTurnout] | None:
    """Load stored turnout CSVs for audit when present under ``data/elections/{source}/{id}/``."""
    paths = stored_turnout_paths(data_dir, source, election_id)
    if not paths:
        return None

    turnout_rows: list[CountyTurnout] = []
    for path in paths:
        if report_dates is not None:
            file_date = _date_from_turnout_filename(path, source)
            if file_date is not None and file_date not in report_dates:
                continue
        turnout_rows.extend(read_turnout_csv(path))

    return turnout_rows if turnout_rows else None


def write_turnout_csv(rows: list[CountyTurnout], path: Path) -> Path:
    """Write county turnout rows to a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_TURNOUT_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "election_id": row.election_id,
                    "report_date": row.report_date.isoformat(),
                    "county": row.county,
                    "county_id": row.county_id if row.county_id is not None else "",
                    "registered_voters": row.registered_voters,
                    "in_person_votes_on_date": row.in_person_votes_on_date,
                    "total_in_person_votes": row.total_in_person_votes,
                    "total_mail_votes": row.total_mail_votes,
                    "roster_available": str(row.roster_available).lower(),
                    "source": row.source,
                }
            )
    return path

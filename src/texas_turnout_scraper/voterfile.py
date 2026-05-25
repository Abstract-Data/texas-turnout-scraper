"""Voterfile matching — join a Texas statewide voterfile against an EV roster.

Uses DuckDB to query the voterfile CSV directly with SQL — no need to load the
full multi-GB file into memory.  DuckDB scans only the rows it needs.

## Workflow

1. ``detect_columns(voterfile_path)`` — scan the header and return a best-guess
   ``ColumnMapping`` via fuzzy + prefix pattern matching.
2. User confirms/corrects the mapping interactively (handled in ``cli.py``).
3. ``match_voterfile_to_roster(roster_records, voterfile_path, mapping)`` —
   runs a DuckDB IN-join against the voterfile CSV and returns a list of
   ``EnrichedVoterRecord`` plus a ``VoterfileMatchReport``.
4. ``write_enriched_csv(records, path)`` — writes the enriched roster to CSV.

## Age brackets

    18-24, 25-34, 35-44, 45-54, 55-64, 65-74, 75+

DOB is expected as YYYYMMDD (Texas state voterfile format) or YYYY-MM-DD.

## PII constraints

``voter_name`` and ``id_voter`` from VoterRecord must not be logged at any
level.  They are written to output CSV (public record) but never to logs or
exception messages.  Voterfile name/DOB fields are likewise not logged.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from .models import (
    AuditFinding,
    ColumnMapping,
    EnrichedVoterRecord,
    VoterfileMatchReport,
    VoterRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Age brackets
# ---------------------------------------------------------------------------

_AGE_BRACKETS: list[tuple[int, int, str]] = [
    (18, 24, "18-24"),
    (25, 34, "25-34"),
    (35, 44, "35-44"),
    (45, 54, "45-54"),
    (55, 64, "55-64"),
    (65, 74, "65-74"),
    (75, 999, "75+"),
]


def age_bracket(dob_raw: str, reference_date: date | None = None) -> str | None:
    """Return an age bracket string for a raw DOB value.

    Args:
        dob_raw: Raw DOB string — accepts ``YYYYMMDD`` (Texas state format) or
            ``YYYY-MM-DD`` or ``MM/DD/YYYY``.
        reference_date: Date to calculate age against.  Defaults to today.

    Returns:
        Bracket label e.g. ``"18-24"`` or ``None`` for blank/unparseable values.
    """
    if not dob_raw or not str(dob_raw).strip():
        return None
    raw = str(dob_raw).strip()
    ref = reference_date or date.today()
    dob: date | None = None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            dob = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if dob is None:
        return None
    age = ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))
    for low, high, label in _AGE_BRACKETS:
        if low <= age <= high:
            return label
    return None  # under 18 or implausible DOB


def normalize_precinct(precinct: str) -> str:
    """Normalize a precinct code for roster vs voterfile comparison.

    Texas SOS voterfiles zero-pad numeric precincts (e.g. ``0510``) while Civix
    rosters often omit leading zeros (``510``).  All-digit values are compared
    after ``zfill(4)``; alphanumeric precincts use stripped upper-case text.
    """
    raw = precinct.strip().upper()
    if not raw:
        return ""
    if raw.isdigit():
        return raw.zfill(4)
    digits_only = "".join(c for c in raw if c.isdigit())
    if digits_only and not any(c.isalpha() for c in raw):
        return digits_only.zfill(4)
    return raw


def precincts_match(roster_precinct: str, vf_precinct: str) -> bool:
    """Return True when roster and voterfile precinct values refer to the same precinct."""
    return normalize_precinct(roster_precinct) == normalize_precinct(vf_precinct)


# ---------------------------------------------------------------------------
# Column auto-detection
# ---------------------------------------------------------------------------

_FIELD_PATTERNS: dict[str, list[str]] = {
    "vuid": [
        "vuid",
        "idvoter",
        "voterid",
        "vanid",
        "lalvoterid",
        "registrantid",
        "statecodevoterid",
    ],
    "cd": ["cd", "congdist", "congressional", "ushouse", "conghouse"],
    "hd": ["hd", "housedist", "statehouse", "lowerchamber"],
    "sd": ["sd", "senatedist", "statesenate", "upperchamber"],
    "county": ["county", "countyname", "co"],
    "precinct": ["pct", "precinct", "precinctcode", "pctcode", "precinctnum"],
    "last_name": ["lname", "lastname", "last", "surname"],
    "first_name": ["fname", "firstname", "first"],
    "full_name": ["fullname", "votername", "name"],
    "dob": ["dob", "dateofbirth", "birthdate", "birthdt", "birth"],
    "sex": ["sex", "gender"],
    "hispanic": ["hispanic", "hisp", "ethnicity"],
    "status": ["status", "voterstatus", "regstatus"],
}

# Columns that may START with a short prefix (e.g. CDPLANC2333, HD2022, SD2022)
_FIELD_PREFIX_PATTERNS: dict[str, list[str]] = {
    "cd": ["cd"],
    "hd": ["hd"],
    "sd": ["sd"],
}

_CONFIDENCE_EXACT = "✓ Exact"
_CONFIDENCE_PREFIX = "~ Prefix"
_CONFIDENCE_PATTERN = "✓ Pattern"
_CONFIDENCE_NONE = "✗ Not detected"


def _normalise(col: str) -> str:
    return re.sub(r"[\s_\-]", "", col.lower())


def _check_prefix(norm: str, field: str) -> bool:
    for prefix in _FIELD_PREFIX_PATTERNS.get(field, []):
        if norm.startswith(prefix) and len(norm) > len(prefix):
            remainder = norm[len(prefix) :]
            # Must start with digit or known word (plan, dist) — not another letter
            if remainder[0].isdigit() or remainder[:4] in ("plan", "dist"):
                return True
    return False


def detect_columns(voterfile_path: Path) -> tuple[ColumnMapping, dict[str, str]]:
    """Scan the voterfile header and return a best-guess ColumnMapping.

    Reads only the first CSV line (the header).  Handles UTF-8 BOM.

    Returns:
        (ColumnMapping, confidence_map) — confidence_map maps each standard
        field name to a human-readable confidence string for display.
    """
    with voterfile_path.open("r", newline="", encoding="utf-8-sig") as fh:
        columns: list[str] = next(csv.reader(fh))

    norm_map: dict[str, str] = {_normalise(c): c for c in columns}

    mapping_kwargs: dict[str, str | None] = {}
    confidence: dict[str, str] = {}

    for field in _FIELD_PATTERNS:
        matched: str | None = None
        conf = _CONFIDENCE_NONE

        # 1. Near-exact: normalised column == normalised field name
        field_norm = _normalise(field.replace("_", ""))
        if field_norm in norm_map:
            matched = norm_map[field_norm]
            conf = _CONFIDENCE_EXACT

        # 2. Known pattern list
        if matched is None:
            for norm, original in norm_map.items():
                if norm in _FIELD_PATTERNS.get(field, []):
                    matched = original
                    conf = _CONFIDENCE_PATTERN
                    break

        # 3. Prefix pattern (CD*, HD*, SD*)
        if matched is None:
            for norm, original in norm_map.items():
                if _check_prefix(norm, field):
                    matched = original
                    conf = _CONFIDENCE_PREFIX
                    break

        mapping_kwargs[field] = matched
        confidence[field] = conf if matched else _CONFIDENCE_NONE

    return ColumnMapping(**mapping_kwargs), confidence


def list_voterfile_columns(voterfile_path: Path) -> list[str]:
    """Return the raw column names from the voterfile header."""
    with voterfile_path.open("r", newline="", encoding="utf-8-sig") as fh:
        return next(csv.reader(fh))


# ---------------------------------------------------------------------------
# Mapping persistence
# ---------------------------------------------------------------------------


def save_mapping(mapping: ColumnMapping, sidecar_path: Path) -> None:
    """Persist a ColumnMapping to a JSON sidecar file."""
    sidecar_path.write_text(
        json.dumps(mapping.model_dump(), indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("Column mapping saved to %s", sidecar_path)


def load_mapping(sidecar_path: Path) -> ColumnMapping:
    """Load a ColumnMapping from a JSON sidecar file."""
    return ColumnMapping(**json.loads(sidecar_path.read_text(encoding="utf-8")))


def sidecar_path_for(voterfile_path: Path) -> Path:
    """Return the default sidecar JSON path for a voterfile."""
    return voterfile_path.with_suffix(".mapping.json")


# ---------------------------------------------------------------------------
# Core match — DuckDB
# ---------------------------------------------------------------------------


def _quote_sql_identifier(col: str) -> str:
    """Double-quote a CSV column name for safe DuckDB SQL embedding.

    Embedded double quotes are escaped per SQL identifier rules (``"`` → ``""``).
    Empty names are rejected — they cannot refer to a real CSV header.
    """
    if not col:
        raise ValueError("Column mapping column name must not be empty")
    return '"' + col.replace('"', '""') + '"'


def match_voterfile_to_roster(
    roster_records: list[VoterRecord],
    voterfile_path: Path,
    mapping: ColumnMapping,
    reference_date: date | None = None,
    progress_callback: callable | None = None,
) -> tuple[list[EnrichedVoterRecord], VoterfileMatchReport]:
    """Join an EV roster against a voterfile using DuckDB.

    DuckDB reads the CSV directly from disk with an optimised scan — no need
    to load the multi-GB file into memory.  The roster VUID set is passed as a
    parameterised list so DuckDB can push the filter down to the scan.

    Args:
        roster_records: Flat list of VoterRecord (output of ``accumulate_roster()``).
        voterfile_path: Path to the voterfile CSV.
        mapping: Column mapping (from ``detect_columns()`` or user input).
        reference_date: Date for age bracket calculation.  Defaults to today.
        progress_callback: Optional callable() called once after the DuckDB
            query completes (single-pass — no chunk callbacks).

    Returns:
        (enriched_records, match_report)

    Raises:
        ImportError: If ``duckdb`` is not installed.
        ValueError: If ``mapping.vuid`` is not set.
    """
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(
            "duckdb is required for voterfile matching. Install it with: pip install duckdb"
        ) from exc

    if mapping.vuid is None:
        raise ValueError(
            "ColumnMapping.vuid must be set before matching. "
            "Run detect_columns() or configure it interactively."
        )

    ref = reference_date or date.today()

    # Zero-pad roster VUIDs; dedupe for the DuckDB IN filter (order preserved)
    roster_vuids: list[str] = list(dict.fromkeys(r.id_voter.zfill(10) for r in roster_records))

    # Determine which voterfile columns to SELECT
    field_to_col: dict[str, str] = {}
    for field in (
        "vuid",
        "cd",
        "hd",
        "sd",
        "county",
        "precinct",
        "dob",
        "sex",
        "hispanic",
        "status",
        "first_name",
        "last_name",
        "full_name",
    ):
        col = getattr(mapping, field)
        if col:
            field_to_col[field] = col

    vuid_col = mapping.vuid
    select_cols = list(dict.fromkeys(field_to_col.values()))  # deduplicated, ordered

    # Build SQL — quote/escape column names before embedding in identifiers
    quoted_cols = ", ".join(_quote_sql_identifier(c) for c in select_cols)
    quoted_vuid_col = _quote_sql_identifier(vuid_col)
    vf_path_str = str(voterfile_path).replace("'", "''")

    sql = f"""
        SELECT {quoted_cols}
        FROM read_csv(
            '{vf_path_str}',
            header = true,
            all_varchar = true,
            encoding = 'utf-8'
        )
        WHERE lpad(trim(cast({quoted_vuid_col} as varchar)), 10, '0') IN (SELECT unnest(?))
    """

    logger.info(
        "Running DuckDB scan on %s — filtering %d roster VUIDs",
        voterfile_path.name,
        len(roster_vuids),
    )

    conn = duckdb.connect()
    rows = conn.execute(sql, [roster_vuids]).fetchall()
    col_names = [desc[0] for desc in conn.description]
    conn.close()

    if progress_callback:
        progress_callback()

    logger.info("DuckDB returned %d matching voterfile rows", len(rows))

    # Build VUID → voterfile row dict (first row wins on duplicate VUIDs)
    vf_lookup: dict[str, dict] = {}
    duplicate_vf_rows = 0
    for row in rows:
        row_dict = dict(zip(col_names, row, strict=True))
        vuid_raw = str(row_dict.get(vuid_col, "") or "").strip().zfill(10)
        if not vuid_raw:
            continue
        if vuid_raw in vf_lookup:
            duplicate_vf_rows += 1
        else:
            vf_lookup[vuid_raw] = row_dict

    # Build enriched records
    enriched: list[EnrichedVoterRecord] = []

    def _get(row_dict: dict | None, field: str) -> str | None:
        col = field_to_col.get(field)
        if col is None or row_dict is None:
            return None
        val = str(row_dict.get(col) or "").strip()
        return val if val and val.upper() != "NULL" else None

    for rec in roster_records:
        vuid_padded = rec.id_voter.zfill(10)
        vf_row = vf_lookup.get(vuid_padded)

        dob_raw = _get(vf_row, "dob")
        bracket = age_bracket(dob_raw, ref) if dob_raw else None

        enriched.append(
            EnrichedVoterRecord(
                id_voter=rec.id_voter,
                voting_method=rec.voting_method,
                precinct=rec.precinct,
                county=rec.county,
                election_id=rec.election_id,
                report_date=rec.report_date,
                voter_name=rec.voter_name,  # PII — do not log
                duplicate_flag=rec.duplicate_flag,
                duplicate_type=rec.duplicate_type,
                also_found_on=rec.also_found_on,
                in_voterfile=vf_row is not None,
                cd=_get(vf_row, "cd"),
                hd=_get(vf_row, "hd"),
                sd=_get(vf_row, "sd"),
                vf_county=_get(vf_row, "county"),
                vf_precinct=_get(vf_row, "precinct"),
                age_bracket=bracket,
                sex=_get(vf_row, "sex"),
                hispanic=_get(vf_row, "hispanic"),
                voter_status=_get(vf_row, "status"),
            )
        )

    # Build match report
    matched = [r for r in enriched if r.in_voterfile]
    total = len(roster_records)
    matched_count = len(matched)
    unmatched_count = total - matched_count
    match_rate = matched_count / total if total else 0.0

    def _count(attr: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in matched:
            val = str(getattr(r, attr) or "Unknown").strip() or "Unknown"
            counts[val] = counts.get(val, 0) + 1
        return dict(sorted(counts.items()))

    def _count_method() -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in matched:
            val = r.voting_method.value
            counts[val] = counts.get(val, 0) + 1
        return dict(sorted(counts.items()))

    findings: list[AuditFinding] = []

    if unmatched_count > 0:
        pct = (unmatched_count / total * 100) if total else 0
        findings.append(
            AuditFinding(
                finding_type="unmatched_voters",
                severity="warning",
                detail=(
                    f"{unmatched_count} EV roster records ({pct:.1f}%) not found in voterfile "
                    f"— possible stale voterfile or out-of-district voters"
                ),
            )
        )

    county_mismatches = sum(
        1 for r in matched if r.vf_county and r.county.upper() != r.vf_county.upper()
    )
    if county_mismatches:
        findings.append(
            AuditFinding(
                finding_type="county_mismatch",
                severity="warning",
                detail=f"{county_mismatches} matched records: county differs between EV roster and voterfile",
            )
        )

    precinct_mismatches = sum(
        1
        for r in matched
        if r.vf_precinct and r.precinct.strip() and not precincts_match(r.precinct, r.vf_precinct)
    )
    if precinct_mismatches:
        findings.append(
            AuditFinding(
                finding_type="precinct_mismatch",
                severity="info",
                detail=f"{precinct_mismatches} matched records: precinct differs between EV roster and voterfile",
            )
        )

    if duplicate_vf_rows:
        findings.append(
            AuditFinding(
                finding_type="duplicate_voterfile_vuids",
                severity="info",
                detail=(
                    f"{duplicate_vf_rows} extra voterfile row(s) with duplicate VUIDs "
                    f"were skipped (first row kept per VUID)"
                ),
            )
        )

    total_vf_rows = count_voterfile_rows(voterfile_path)

    report = VoterfileMatchReport(
        election_id=roster_records[0].election_id if roster_records else "unknown",
        report_date=roster_records[0].report_date if roster_records else ref,
        voterfile_path=str(voterfile_path),
        roster_path="",  # filled in by CLI
        total_roster_records=total,
        total_voterfile_records=total_vf_rows,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        match_rate=match_rate,
        by_cd=_count("cd"),
        by_hd=_count("hd"),
        by_sd=_count("sd"),
        by_county=_count("county"),
        by_age_bracket=_count("age_bracket"),
        by_sex=_count("sex"),
        by_voting_method=_count_method(),
        by_hispanic=_count("hispanic"),
        findings=findings,
    )

    logger.info(
        "Match complete: %d/%d records matched (%.1f%%)",
        matched_count,
        total,
        match_rate * 100,
    )
    return enriched, report


def count_voterfile_rows(voterfile_path: Path) -> int:
    """Return the approximate row count of the voterfile using DuckDB.

    This is fast — DuckDB counts without parsing every cell.
    """
    try:
        import duckdb
    except ImportError:
        return 0
    vf_path_str = str(voterfile_path).replace("'", "''")
    conn = duckdb.connect()
    result = conn.execute(
        f"SELECT COUNT(*) FROM read_csv('{vf_path_str}', header=true, all_varchar=true)"
    ).fetchone()
    conn.close()
    return result[0] if result else 0


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

_ENRICHED_COLUMNS = [
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
    "IN_VOTERFILE",
    "CD",
    "HD",
    "SD",
    "VF_COUNTY",
    "VF_PRECINCT",
    "AGE_BRACKET",
    "SEX",
    "HISPANIC",
    "VOTER_STATUS",
]


def write_enriched_csv(records: list[EnrichedVoterRecord], path: Path) -> Path:
    """Write enriched records to a CSV file.

    Args:
        records: Output of ``match_voterfile_to_roster()``.
        path: Destination path.

    Returns:
        The path written to.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_ENRICHED_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for rec in records:
            writer.writerow(
                {
                    "VOTER_NAME": rec.voter_name,  # PII — public record
                    "ID_VOTER": rec.id_voter,
                    "VOTING_METHOD": rec.voting_method.value,
                    "PRECINCT": rec.precinct,
                    "COUNTY": rec.county,
                    "ELECTION_ID": rec.election_id,
                    "REPORT_DATE": rec.report_date.isoformat(),
                    "DUPLICATE_FLAG": str(rec.duplicate_flag).lower(),
                    "DUPLICATE_TYPE": rec.duplicate_type,
                    "ALSO_FOUND_ON": rec.also_found_on,
                    "IN_VOTERFILE": str(rec.in_voterfile).lower(),
                    "CD": rec.cd or "",
                    "HD": rec.hd or "",
                    "SD": rec.sd or "",
                    "VF_COUNTY": rec.vf_county or "",
                    "VF_PRECINCT": rec.vf_precinct or "",
                    "AGE_BRACKET": rec.age_bracket or "",
                    "SEX": rec.sex or "",
                    "HISPANIC": rec.hispanic or "",
                    "VOTER_STATUS": rec.voter_status or "",
                }
            )
    return path


def write_match_report_json(report: VoterfileMatchReport, path: Path) -> Path:
    """Write a VoterfileMatchReport to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path

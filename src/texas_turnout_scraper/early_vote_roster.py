"""EV roster canonicalization before hashing and D1.

Every column is kept (owner decision, docs/adr/009-sqlmodel-roster-models.md). Canonical bytes
are the file re-serialized with minimal quoting, so the five-column portal file hashes the same
as it did under the earlier allowlist redaction.

Row-level bytes are never logged. Callers use :func:`summarize_roster_csv` for aggregates only
(counts, column names, method rollups). This module does not import or join voterfile data
(Tex. Elec. Code §18.066).
The geography tables (``geo_load``) place roster precincts on the state's precinct map; they
read only the roster's county and precinct columns and do not use voterfile data either.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

VotePeriod = Literal["early", "election_day"]
VOTE_PERIOD_EARLY: VotePeriod = "early"
VOTE_PERIOD_ELECTION_DAY: VotePeriod = "election_day"
VOTE_PERIODS: tuple[VotePeriod, ...] = (VOTE_PERIOD_EARLY, VOTE_PERIOD_ELECTION_DAY)

# The Election Day report names the first column COUNTY_NAME; stored rosters use COUNTY.
# Renaming before hashing keeps one header across every day (ADR-018).
HEADER_RENAMES: dict[str, str] = {"COUNTY_NAME": "COUNTY"}

# A county reporting under this share of its mean early-day rows is flagged (issue #151). The
# live 2026-11-03 load separated at 0.0007 to 0.0057 for the unsubmitted counties against 0.92
# for the lowest healthy one. Counties with a smaller baseline are too noisy to judge.
ELECTION_DAY_MIN_SHARE = 0.10
COVERAGE_MIN_BASELINE_ROWS = 100


@dataclass(frozen=True)
class RosterCsvSummary:
    """Aggregate metadata only — safe for logs and CI output."""

    columns: tuple[str, ...]
    est_rows: int
    voting_method_counts: tuple[tuple[str, int], ...]
    county_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CountyShortfall:
    """A county whose roster has far fewer rows than its early-day baseline (counts only)."""

    county: str
    rows: int
    baseline_rows: int


def canonicalize_roster_csv(decoded: bytes) -> tuple[bytes, RosterCsvSummary]:
    """All columns and cells, positionally, without a BOM, blank lines, or redundant quotes.

    The returned bytes are what must be hashed and written to D1.
    """
    reader = csv.reader(io.StringIO(decoded.decode("utf-8-sig")))
    header = next(reader, None)
    if header is None:
        return b"", RosterCsvSummary(
            columns=(), est_rows=0, voting_method_counts=(), county_counts=()
        )
    normalized = [name.strip().upper() for name in header]
    method_at = normalized.index("VOTING_METHOD") if "VOTING_METHOD" in normalized else None
    county_at = next(
        (i for i, name in enumerate(normalized) if HEADER_RENAMES.get(name, name) == "COUNTY"),
        None,
    )

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    header = [
        HEADER_RENAMES.get(name, original)
        for name, original in zip(normalized, header, strict=True)
    ]
    writer.writerow(header)
    method_counts: dict[str, int] = {}
    county_counts: dict[str, int] = {}
    rows = 0
    for row in reader:
        if not row:
            continue
        rows += 1
        if method_at is not None and method_at < len(row):
            method = row[method_at].strip()
            if method:
                method_counts[method] = method_counts.get(method, 0) + 1
        if county_at is not None and county_at < len(row):
            county = row[county_at].strip()
            if county:
                county_counts[county] = county_counts.get(county, 0) + 1
        writer.writerow(row)

    summary = RosterCsvSummary(
        columns=tuple(header),
        est_rows=rows,
        voting_method_counts=tuple(sorted(method_counts.items())),
        county_counts=tuple(sorted(county_counts.items())),
    )
    return out.getvalue().encode("utf-8"), summary


def summarize_roster_csv(decoded: bytes) -> RosterCsvSummary:
    """Return aggregates for a roster payload (no logging of row bodies)."""
    _, summary = canonicalize_roster_csv(decoded)
    return summary


def low_coverage_counties(
    day: Mapping[str, int],
    baseline: Mapping[str, float],
    *,
    min_share: float = ELECTION_DAY_MIN_SHARE,
    min_baseline: int = COVERAGE_MIN_BASELINE_ROWS,
) -> tuple[CountyShortfall, ...]:
    """Counties with a usable baseline whose ``day`` rows fall under ``min_share`` of it."""
    return tuple(
        CountyShortfall(county=county, rows=day.get(county, 0), baseline_rows=round(mean))
        for county, mean in sorted(baseline.items())
        if mean >= min_baseline and day.get(county, 0) < min_share * mean
    )

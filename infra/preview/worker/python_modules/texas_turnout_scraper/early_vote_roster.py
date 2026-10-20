"""EV roster allowlist and redaction before D1 (SOS Advisory 2024-20).

Row-level bytes are never logged. Callers use :func:`summarize_roster_csv` for
aggregates only (counts, column names, method rollups).

This module does not import or join voterfile data (Tex. Elec. Code §18.066).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Final

# Per-county Civix CSV (getFileByFormat).
COUNTY_ROSTER_COLUMNS: Final[frozenset[str]] = frozenset(
    {"VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT"}
)

# Statewide early-vote CSV (EVR_STATEWIDE).
STATEWIDE_ROSTER_COLUMNS: Final[frozenset[str]] = COUNTY_ROSTER_COLUMNS | frozenset({"COUNTY"})

# Drop vote-site / polling-location columns if Civix adds them later.
_FORBIDDEN_COLUMN_RE = re.compile(
    r"(POLL|POLLING|VOTE[_\s-]?SITE|VOTING[_\s-]?LOCATION|BALLOT[_\s-]?STYLE)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RosterCsvSummary:
    """Aggregate metadata only — safe for logs and CI output."""

    columns: tuple[str, ...]
    dropped_columns: tuple[str, ...]
    est_rows: int
    voting_method_counts: tuple[tuple[str, int], ...]


def allowlist_for_statewide(statewide: bool) -> frozenset[str]:
    """Columns permitted in D1 for county vs statewide roster payloads."""
    return STATEWIDE_ROSTER_COLUMNS if statewide else COUNTY_ROSTER_COLUMNS


def is_forbidden_roster_column(name: str) -> bool:
    """True when a header must not be stored (polling place / vote site)."""
    stripped = name.strip()
    if not stripped:
        return True
    if _FORBIDDEN_COLUMN_RE.search(stripped):
        return True
    return False


def redact_roster_csv(decoded: bytes, *, statewide: bool) -> tuple[bytes, RosterCsvSummary]:
    """Keep allowlisted columns only; drop polling-location headers and values.

    The returned bytes are what must be hashed and written to D1.
    """
    allow = allowlist_for_statewide(statewide)
    text = decoded.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        empty = RosterCsvSummary(
            columns=(),
            dropped_columns=(),
            est_rows=0,
            voting_method_counts=(),
        )
        return b"", empty

    original_headers = [h for h in reader.fieldnames if h is not None]
    kept: list[str] = []
    dropped: list[str] = []
    for header in original_headers:
        if is_forbidden_roster_column(header):
            dropped.append(header)
            continue
        if header in allow:
            kept.append(header)
        else:
            dropped.append(header)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=kept, lineterminator="\n")
    writer.writeheader()
    method_counts: dict[str, int] = {}
    rows = 0
    for row in reader:
        rows += 1
        filtered = {key: row.get(key, "") for key in kept}
        method = (filtered.get("VOTING_METHOD") or "").strip()
        if method:
            method_counts[method] = method_counts.get(method, 0) + 1
        writer.writerow(filtered)

    summary = RosterCsvSummary(
        columns=tuple(kept),
        dropped_columns=tuple(dropped),
        est_rows=rows,
        voting_method_counts=tuple(sorted(method_counts.items())),
    )
    return out.getvalue().encode("utf-8"), summary


def summarize_roster_csv(decoded: bytes, *, statewide: bool) -> RosterCsvSummary:
    """Return aggregates for a roster payload (no logging of row bodies)."""
    _, summary = redact_roster_csv(decoded, statewide=statewide)
    return summary

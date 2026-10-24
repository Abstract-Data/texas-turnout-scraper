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
from dataclasses import dataclass


@dataclass(frozen=True)
class RosterCsvSummary:
    """Aggregate metadata only — safe for logs and CI output."""

    columns: tuple[str, ...]
    est_rows: int
    voting_method_counts: tuple[tuple[str, int], ...]


def canonicalize_roster_csv(decoded: bytes) -> tuple[bytes, RosterCsvSummary]:
    """All columns and cells, positionally, without a BOM, blank lines, or redundant quotes.

    The returned bytes are what must be hashed and written to D1.
    """
    reader = csv.reader(io.StringIO(decoded.decode("utf-8-sig")))
    header = next(reader, None)
    if header is None:
        return b"", RosterCsvSummary(columns=(), est_rows=0, voting_method_counts=())
    normalized = [name.strip().upper() for name in header]
    method_at = normalized.index("VOTING_METHOD") if "VOTING_METHOD" in normalized else None

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    method_counts: dict[str, int] = {}
    rows = 0
    for row in reader:
        if not row:
            continue
        rows += 1
        if method_at is not None and method_at < len(row):
            method = row[method_at].strip()
            if method:
                method_counts[method] = method_counts.get(method, 0) + 1
        writer.writerow(row)

    summary = RosterCsvSummary(
        columns=tuple(header),
        est_rows=rows,
        voting_method_counts=tuple(sorted(method_counts.items())),
    )
    return out.getvalue().encode("utf-8"), summary


def summarize_roster_csv(decoded: bytes) -> RosterCsvSummary:
    """Return aggregates for a roster payload (no logging of row bodies)."""
    _, summary = canonicalize_roster_csv(decoded)
    return summary

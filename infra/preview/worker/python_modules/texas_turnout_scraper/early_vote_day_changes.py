"""Voters added or removed when a day's roster file changes between loads.

Rows carry the VUID, county and voting method only. Voter names never reach
this table, and nothing here logs a VUID.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from texas_turnout_scraper.early_vote_aggregates import detect_payload_kind

DAY_CHANGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_day_change (
    source_election_id TEXT NOT NULL,
    ev_date TEXT NOT NULL,
    old_sha256 TEXT NOT NULL,
    new_sha256 TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    id_voter TEXT NOT NULL,
    county TEXT NOT NULL,
    voting_method TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (source_election_id, ev_date, new_sha256, change_kind, id_voter)
)
""".strip()

_INSERT_PREFIX = (
    "INSERT OR IGNORE INTO early_vote_day_change (source_election_id, ev_date, old_sha256, "
    "new_sha256, change_kind, id_voter, county, voting_method, recorded_at) VALUES "
)
_ROW_PLACEHOLDERS = "(?, ?, ?, ?, ?, ?, ?, ?, ?)"
_PARAMS_PER_ROW = 9
# D1 allows 100 bound parameters per statement.
_ROWS_PER_STATEMENT = 100 // _PARAMS_PER_ROW


@dataclass(frozen=True)
class VoterRef:
    id_voter: str
    county: str
    voting_method: str


@dataclass(frozen=True)
class RosterDiff:
    added: tuple[VoterRef, ...]
    removed: tuple[VoterRef, ...]


def _voters(decoded: bytes) -> dict[str, VoterRef]:
    reader = csv.DictReader(io.StringIO(decoded.decode("utf-8-sig")))
    voters: dict[str, VoterRef] = {}
    for row in reader:
        id_voter = str(row.get("ID_VOTER") or "").strip().zfill(10)
        if id_voter == "0000000000":
            continue
        voters[id_voter] = VoterRef(
            id_voter=id_voter,
            county=str(row.get("COUNTY") or "").strip().upper(),
            voting_method=str(row.get("VOTING_METHOD") or "").strip().upper(),
        )
    return voters


def diff_rosters(old: bytes, new: bytes) -> RosterDiff | None:
    """Compare two statewide roster CSVs by VUID, or None for non-roster payloads."""
    if (
        detect_payload_kind(old) != "statewide_roster"
        or detect_payload_kind(new) != "statewide_roster"
    ):
        return None
    before = _voters(old)
    after = _voters(new)
    return RosterDiff(
        added=tuple(after[key] for key in sorted(after.keys() - before.keys())),
        removed=tuple(before[key] for key in sorted(before.keys() - after.keys())),
    )


def change_statements(
    source_election_id: str,
    ev_date: str,
    *,
    old_sha256: str,
    new_sha256: str,
    diff: RosterDiff,
    recorded_at: str,
) -> list[tuple[str, list[str]]]:
    """Multi-row ``INSERT OR IGNORE`` statements, so a retried load adds nothing twice."""
    tagged = [("added", item) for item in diff.added] + [("removed", item) for item in diff.removed]
    statements: list[tuple[str, list[str]]] = []
    for start in range(0, len(tagged), _ROWS_PER_STATEMENT):
        batch = tagged[start : start + _ROWS_PER_STATEMENT]
        params: list[str] = []
        for kind, item in batch:
            params.extend(
                [
                    source_election_id,
                    ev_date,
                    old_sha256,
                    new_sha256,
                    kind,
                    item.id_voter,
                    item.county,
                    item.voting_method,
                    recorded_at,
                ]
            )
        sql = _INSERT_PREFIX + ", ".join([_ROW_PLACEHOLDERS] * len(batch))
        statements.append((sql, params))
    return statements


def roster_row_count(decoded: bytes) -> int:
    """Distinct VUIDs in a roster CSV, or 0 for other payloads."""
    if detect_payload_kind(decoded) != "statewide_roster":
        return 0
    return len(_voters(decoded))

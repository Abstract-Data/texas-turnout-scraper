"""Loader-side D1 aggregate table DDL and write statements (no D1 read imports)."""

from __future__ import annotations

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_aggregates import aggregates_from_decoded

Statement = tuple[str, tuple[str, ...]]

_FILE_AGG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_file_aggregate (
    source_election_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    payload_kind TEXT NOT NULL,
    ev_date TEXT,
    statewide_total INTEGER NOT NULL,
    statewide_in_person INTEGER NOT NULL,
    statewide_mail_in INTEGER NOT NULL,
    PRIMARY KEY (source_election_id, file_id)
)
""".strip()

_COUNTY_AGG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_county_aggregate (
    source_election_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    county TEXT NOT NULL,
    total INTEGER NOT NULL,
    in_person INTEGER NOT NULL,
    mail_in INTEGER NOT NULL,
    PRIMARY KEY (source_election_id, file_id, county)
)
""".strip()

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_aggregate_meta (
    source_election_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    county_count INTEGER NOT NULL,
    PRIMARY KEY (source_election_id, file_id)
)
""".strip()

_FILE_AGG_DELETE_SQL = (
    "DELETE FROM early_vote_file_aggregate WHERE source_election_id = ? AND file_id = ?"
)
_COUNTY_AGG_DELETE_SQL = (
    "DELETE FROM early_vote_county_aggregate WHERE source_election_id = ? AND file_id = ?"
)
_META_DELETE_SQL = (
    "DELETE FROM early_vote_aggregate_meta WHERE source_election_id = ? AND file_id = ?"
)
_META_INSERT_SQL = (
    "INSERT INTO early_vote_aggregate_meta (source_election_id, file_id, county_count) "
    "VALUES (?, ?, ?)"
)
_FILE_AGG_INSERT_SQL = (
    "INSERT INTO early_vote_file_aggregate "
    "(source_election_id, file_id, payload_kind, ev_date, "
    "statewide_total, statewide_in_person, statewide_mail_in) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_COUNTY_AGG_INSERT_SQL = (
    "INSERT INTO early_vote_county_aggregate "
    "(source_election_id, file_id, county, total, in_person, mail_in) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)

PRECOMPUTE_SCHEMA_CREATES: tuple[str, ...] = (
    _FILE_AGG_TABLE_SQL,
    _COUNTY_AGG_TABLE_SQL,
    _META_TABLE_SQL,
)


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def aggregate_body_from_decoded(
    decoded: bytes,
    *,
    source_election_id: str,
    ev_date_iso: str | None = None,
) -> dict[str, object]:
    """Build the same JSON shape the Worker serves (for loader + parity tests)."""
    return aggregates_from_decoded(
        decoded,
        source_election_id=source_election_id,
        ev_date=ev_date_iso,
    )


def precompute_row_count(body: dict[str, object]) -> int:
    """County aggregate rows plus one file-level row."""
    by_county = body.get("by_county")
    if not isinstance(by_county, list):
        return 1
    return 1 + len(by_county)


def precompute_statements(
    election_id: str,
    file_id: str,
    body: dict[str, object],
    *,
    ev_date_iso: str | None = None,
) -> tuple[Statement, ...]:
    """Delete-then-insert precomputed rows for one published file (idempotent)."""
    election_id = require_election_id(election_id)
    if not file_id:
        msg = "file_id must be non-empty"
        raise ValueError(msg)
    payload_kind = body.get("payload_kind")
    if not isinstance(payload_kind, str) or not payload_kind:
        msg = "payload_kind missing from aggregate body"
        raise ValueError(msg)
    statewide = body.get("statewide")
    if not isinstance(statewide, dict):
        msg = "statewide missing from aggregate body"
        raise ValueError(msg)
    total = _coerce_int(statewide.get("total")) or 0
    in_person = _coerce_int(statewide.get("in_person")) or 0
    mail_in = _coerce_int(statewide.get("mail_in")) or 0
    resolved_ev = ev_date_iso
    if resolved_ev is None and isinstance(body.get("ev_date"), str):
        resolved_ev = str(body["ev_date"])
    statements: list[Statement] = [
        (_FILE_AGG_DELETE_SQL, (election_id, file_id)),
        (_COUNTY_AGG_DELETE_SQL, (election_id, file_id)),
        (_META_DELETE_SQL, (election_id, file_id)),
    ]
    by_county = body.get("by_county")
    expected_counties = 0
    if isinstance(by_county, list):
        for row in by_county:
            if not isinstance(row, dict):
                continue
            county = row.get("county")
            if not isinstance(county, str) or not county:
                continue
            if county.strip():
                expected_counties += 1
            row_total = _coerce_int(row.get("total")) or 0
            row_ip = _coerce_int(row.get("in_person")) or 0
            row_mail = _coerce_int(row.get("mail_in")) or 0
            statements.append(
                (
                    _COUNTY_AGG_INSERT_SQL,
                    (
                        election_id,
                        file_id,
                        county,
                        str(row_total),
                        str(row_ip),
                        str(row_mail),
                    ),
                )
            )
    statements.append((_META_INSERT_SQL, (election_id, file_id, str(expected_counties))))
    statements.append(
        (
            _FILE_AGG_INSERT_SQL,
            (
                election_id,
                file_id,
                payload_kind,
                resolved_ev or "",
                str(total),
                str(in_person),
                str(mail_in),
            ),
        )
    )
    return tuple(statements)


def delete_precompute_statements(election_id: str, file_id: str) -> tuple[Statement, ...]:
    election_id = require_election_id(election_id)
    return (
        (_COUNTY_AGG_DELETE_SQL, (election_id, file_id)),
        (_META_DELETE_SQL, (election_id, file_id)),
        (_FILE_AGG_DELETE_SQL, (election_id, file_id)),
    )

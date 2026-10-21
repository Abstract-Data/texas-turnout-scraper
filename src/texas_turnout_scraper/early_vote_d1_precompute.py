"""Read loader-written aggregate rows from per-election D1."""

from __future__ import annotations

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_precompute_store import (
    PRECOMPUTE_SCHEMA_CREATES,
    aggregate_body_from_decoded,
    delete_precompute_statements,
    precompute_row_count,
    precompute_statements,
)
from texas_turnout_scraper.early_vote_d1_read import (
    D1Database,
    _d1_fetch_all,
    _d1_fetch_first,
    _d1_int,
    _row_value,
)

_FILE_AGG_SELECT_SQL = (
    "SELECT payload_kind, ev_date, statewide_total, statewide_in_person, "
    "statewide_mail_in FROM early_vote_file_aggregate "
    "WHERE source_election_id = ? AND file_id = ?"
)
_COUNTY_AGG_SELECT_SQL = (
    "SELECT county, total, in_person, mail_in FROM early_vote_county_aggregate "
    "WHERE source_election_id = ? AND file_id = ? ORDER BY county ASC"
)

__all__ = (
    "PRECOMPUTE_SCHEMA_CREATES",
    "aggregate_body_from_decoded",
    "delete_precompute_statements",
    "load_precomputed_body",
    "precompute_row_count",
    "precompute_statements",
)


def _body_from_rows(
    election_id: str,
    file_row: object,
    county_rows: list[object],
    *,
    ev_date: str | None,
) -> dict[str, object] | None:
    payload_kind = _row_value(file_row, "payload_kind")
    if not isinstance(payload_kind, str) or not payload_kind:
        return None
    stored_ev = _row_value(file_row, "ev_date")
    resolved_ev = ev_date
    if resolved_ev is None and isinstance(stored_ev, str) and stored_ev:
        resolved_ev = stored_ev
    total = _d1_int(_row_value(file_row, "statewide_total")) or 0
    in_person = _d1_int(_row_value(file_row, "statewide_in_person")) or 0
    mail_in = _d1_int(_row_value(file_row, "statewide_mail_in")) or 0
    by_county: list[dict[str, object]] = []
    for row in county_rows:
        county = _row_value(row, "county")
        if not isinstance(county, str) or not county:
            continue
        row_total = _d1_int(_row_value(row, "total")) or 0
        row_ip = _d1_int(_row_value(row, "in_person")) or 0
        row_mail = _d1_int(_row_value(row, "mail_in")) or 0
        by_county.append(
            {
                "county": county,
                "total": row_total,
                "in_person": row_ip,
                "mail_in": row_mail,
            }
        )
    body: dict[str, object] = {
        "source_election_id": election_id,
        "payload_kind": payload_kind,
        "statewide": {
            "total": total,
            "in_person": in_person,
            "mail_in": mail_in,
        },
        "by_county": by_county,
    }
    if resolved_ev is not None:
        body["ev_date"] = resolved_ev
    return body


async def load_precomputed_body(
    db: D1Database,
    source_election_id: str,
    file_id: str,
    *,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    """Return aggregate JSON from loader-written rows, or None when missing."""
    election_id = require_election_id(source_election_id)
    file_row = await _d1_fetch_first(
        db.prepare(_FILE_AGG_SELECT_SQL).bind(election_id, file_id),
    )
    if file_row is None:
        return None
    county_rows = await _d1_fetch_all(
        db.prepare(_COUNTY_AGG_SELECT_SQL).bind(election_id, file_id),
    )
    return _body_from_rows(election_id, file_row, county_rows, ev_date=ev_date)

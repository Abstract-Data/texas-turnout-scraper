"""Aggregate early-vote publishes in D1 without loading full chunk payloads into the Worker.

Turnout JSON uses SQLite ``json_each`` on a bound JSON text parameter (county rows
and statewide sums stay in D1). Statewide roster CSV is scanned chunk-by-chunk;
each chunk is decoded in the isolate, split into complete lines, and rolled into
county/method totals without retaining the full file bytes.

Logs must never include voter names or VUIDs.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from dataclasses import dataclass, field
from typing import Literal

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_aggregates import (
    build_by_day_series,
    build_county_by_day_series,
    slice_by_method,
)
from texas_turnout_scraper.early_vote_d1_read import (
    D1Database,
    _d1_fetch_all,
    _d1_fetch_first,
    _d1_int,
    _row_value,
    load_day_rows,
    load_file_views,
    load_pointer_file_id,
    resolve_day_for_date,
)
from texas_turnout_scraper.early_vote_sync import EarlyVoteFileView, choose_published_file

PayloadKind = Literal["turnout", "statewide_roster"]

# Turnout JSON for all Texas counties is far below roster size; cap SQL json_each input.
_MAX_TURNOUT_JSON_BYTES = 8_000_000

_CHUNK_INDEX_SQL = (
    "SELECT chunk_index FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? ORDER BY chunk_index ASC"
)
_ONE_CHUNK_SQL = (
    "SELECT payload_b64 FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? AND chunk_index = ?"
)
_FILE_BYTE_LENGTH_SQL = (
    "SELECT byte_length FROM early_vote_file "
    "WHERE source_election_id = ? AND file_id = ?"
)

_TURNOUT_COUNTY_SQL = """
SELECT
  trim(json_extract(value, '$.name')) AS county,
  CAST(COALESCE(json_extract(value, '$.total_in_person_votes_for_election'), 0) AS INTEGER)
    AS in_person,
  CAST(COALESCE(json_extract(value, '$.total_mail_votes_for_election'), 0) AS INTEGER)
    AS mail_in
FROM json_each(?1, '$.turnout_by_county')
WHERE trim(json_extract(value, '$.name')) != ''
ORDER BY county ASC
""".strip()

_TURNOUT_STATEWIDE_SQL = """
SELECT
  SUM(
    CAST(COALESCE(json_extract(value, '$.total_in_person_votes_for_election'), 0) AS INTEGER)
  ) AS in_person,
  SUM(
    CAST(COALESCE(json_extract(value, '$.total_mail_votes_for_election'), 0) AS INTEGER)
  ) AS mail_in
FROM json_each(?1, '$.turnout_by_county')
""".strip()

_ROSTER_BATCH_SQL = """
SELECT
  upper(trim(json_extract(value, '$.county'))) AS county,
  SUM(
    CASE
      WHEN upper(trim(json_extract(value, '$.method'))) IN (
        'IN-PERSON', 'IN PERSON', 'INPERSON'
      ) THEN 1
      ELSE 0
    END
  ) AS in_person,
  SUM(
    CASE
      WHEN upper(trim(json_extract(value, '$.method'))) IN (
        'MAIL-IN', 'MAIL IN', 'MAILIN', 'MAIL'
      ) THEN 1
      ELSE 0
    END
  ) AS mail_in
FROM json_each(?1)
WHERE trim(json_extract(value, '$.county')) != ''
GROUP BY 1
ORDER BY 1 ASC
""".strip()


@dataclass
class _RosterRollup:
    in_person: int = 0
    mail_in: int = 0
    by_county: dict[str, _RosterRollup] = field(default_factory=dict)

    def add(self, county: str, in_person: int, mail_in: int) -> None:
        self.in_person += in_person
        self.mail_in += mail_in
        bucket = self.by_county.setdefault(county, _RosterRollup())
        bucket.in_person += in_person
        bucket.mail_in += mail_in


async def _published_file(
    db: D1Database,
    election_id: str,
    *,
    kv_sha256: str,
    file_id: str | None,
    expected_sha256: str | None,
) -> EarlyVoteFileView | None:
    if file_id is None:
        pointer = await load_pointer_file_id(db, election_id)
        files = await load_file_views(db, election_id)
        return choose_published_file(
            pointer_file_id=pointer,
            files=files,
            kv_sha256=kv_sha256,
        )
    want_hash = expected_sha256 if expected_sha256 is not None else kv_sha256
    files = await load_file_views(db, election_id)
    for item in files:
        if item.file_id != file_id:
            continue
        if not item.complete or item.sha256 != want_hash:
            return None
        return item
    return None


async def _first_chunk_bytes(
    db: D1Database,
    election_id: str,
    file_id: str,
) -> bytes | None:
    row = await _d1_fetch_first(db.prepare(_ONE_CHUNK_SQL).bind(election_id, file_id, 0))
    if row is None:
        return None
    payload = _row_value(row, "payload_b64")
    if not isinstance(payload, str) or payload == "":
        return None
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError):
        return None


def _kind_from_prefix(prefix: bytes) -> PayloadKind:
    if prefix[:1] in {b"{", b"["}:
        return "turnout"
    return "statewide_roster"


async def detect_payload_kind_from_d1(
    db: D1Database,
    source_election_id: str,
    file_id: str,
) -> PayloadKind:
    election_id = require_election_id(source_election_id)
    head = await _first_chunk_bytes(db, election_id, file_id)
    if head is None:
        return "statewide_roster"
    return _kind_from_prefix(head)


async def _file_byte_length(db: D1Database, election_id: str, file_id: str) -> int | None:
    row = await _d1_fetch_first(db.prepare(_FILE_BYTE_LENGTH_SQL).bind(election_id, file_id))
    if row is None:
        return None
    return _d1_int(_row_value(row, "byte_length"))


async def _chunk_indices(db: D1Database, election_id: str, file_id: str) -> tuple[int, ...]:
    rows = await _d1_fetch_all(db.prepare(_CHUNK_INDEX_SQL).bind(election_id, file_id))
    indices: list[int] = []
    for row in rows:
        index = _d1_int(_row_value(row, "chunk_index"))
        if index is not None:
            indices.append(index)
    indices.sort()
    return tuple(indices)


async def _chunk_b64_at(
    db: D1Database,
    election_id: str,
    file_id: str,
    chunk_index: int,
) -> str | None:
    row = await _d1_fetch_first(
        db.prepare(_ONE_CHUNK_SQL).bind(election_id, file_id, chunk_index)
    )
    if row is None:
        return None
    payload = _row_value(row, "payload_b64")
    return payload if isinstance(payload, str) and payload else None


async def _stream_decode_chunks(
    db: D1Database,
    election_id: str,
    file_id: str,
    *,
    max_total_bytes: int | None = None,
) -> bytes | None:
    """Concatenate chunk payloads with bounded peak memory (one chunk at a time)."""
    parts: list[bytes] = []
    total = 0
    for index in await _chunk_indices(db, election_id, file_id):
        piece_b64 = await _chunk_b64_at(db, election_id, file_id, index)
        if piece_b64 is None:
            return None
        try:
            piece = base64.b64decode(piece_b64)
        except (ValueError, TypeError):
            return None
        total += len(piece)
        if max_total_bytes is not None and total > max_total_bytes:
            return None
        parts.append(piece)
    return b"".join(parts)


def _turnout_body_from_sql_rows(
    *,
    source_election_id: str,
    ev_date: str | None,
    county_rows: list[object],
    statewide_row: object | None,
) -> dict[str, object]:
    election_id = require_election_id(source_election_id)
    by_county: list[dict[str, object]] = []
    for row in county_rows:
        county = _row_value(row, "county")
        ip = _d1_int(_row_value(row, "in_person")) or 0
        mail = _d1_int(_row_value(row, "mail_in")) or 0
        if not isinstance(county, str) or not county:
            continue
        by_county.append(
            {
                "county": county,
                "total": ip + mail,
                "in_person": ip,
                "mail_in": mail,
            }
        )
    statewide_ip = 0
    statewide_mail = 0
    if statewide_row is not None:
        statewide_ip = _d1_int(_row_value(statewide_row, "in_person")) or 0
        statewide_mail = _d1_int(_row_value(statewide_row, "mail_in")) or 0
    body: dict[str, object] = {
        "source_election_id": election_id,
        "payload_kind": "turnout",
        "statewide": {
            "total": statewide_ip + statewide_mail,
            "in_person": statewide_ip,
            "mail_in": statewide_mail,
        },
        "by_county": by_county,
    }
    if ev_date is not None:
        body["ev_date"] = ev_date
    return body


async def aggregate_turnout_from_d1(
    db: D1Database,
    source_election_id: str,
    file_id: str,
    *,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    election_id = require_election_id(source_election_id)
    byte_length = await _file_byte_length(db, election_id, file_id)
    if byte_length is not None and byte_length > _MAX_TURNOUT_JSON_BYTES:
        return None
    decoded = await _stream_decode_chunks(
        db,
        election_id,
        file_id,
        max_total_bytes=_MAX_TURNOUT_JSON_BYTES,
    )
    if decoded is None:
        return None
    try:
        json_text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    county_rows = await _d1_fetch_all(db.prepare(_TURNOUT_COUNTY_SQL).bind(json_text))
    statewide_row = await _d1_fetch_first(db.prepare(_TURNOUT_STATEWIDE_SQL).bind(json_text))
    return _turnout_body_from_sql_rows(
        source_election_id=election_id,
        ev_date=ev_date,
        county_rows=county_rows,
        statewide_row=statewide_row,
    )


def _parse_roster_line(line: str, header_line: str) -> tuple[str, str] | None:
    if not line.strip():
        return None
    reader = csv.DictReader(io.StringIO(f"{header_line}\n{line}"))
    try:
        row = next(reader)
    except StopIteration:
        return None
    county_key = "COUNTY" if "COUNTY" in header_line else "county"
    method_key = "VOTING_METHOD" if "VOTING_METHOD" in header_line else "voting_method"
    county = str(row.get(county_key) or "").strip().upper()
    method = str(row.get(method_key) or "").strip()
    if not county or not method:
        return None
    return county, method


def _lines_from_carry(carry: str, fragment: str) -> tuple[str, list[str]]:
    text = carry + fragment
    if not text:
        return "", []
    parts = text.split("\n")
    if not text.endswith("\n"):
        carry_out = parts.pop()
    else:
        carry_out = ""
    return carry_out, parts


async def _roster_sql_batch(db: D1Database, rows: list[dict[str, str]]) -> list[object]:
    if not rows:
        return []
    payload = json.dumps(rows, separators=(",", ":"))
    return await _d1_fetch_all(db.prepare(_ROSTER_BATCH_SQL).bind(payload))


def _merge_roster_sql_rows(rollup: _RosterRollup, rows: list[object]) -> None:
    for row in rows:
        county = _row_value(row, "county")
        ip = _d1_int(_row_value(row, "in_person")) or 0
        mail = _d1_int(_row_value(row, "mail_in")) or 0
        if not isinstance(county, str) or not county:
            continue
        rollup.add(county, ip, mail)


def _roster_body_from_rollup(
    rollup: _RosterRollup,
    *,
    source_election_id: str,
    ev_date: str | None,
) -> dict[str, object]:
    election_id = require_election_id(source_election_id)
    by_county = [
        {
            "county": name,
            "total": bucket.in_person + bucket.mail_in,
            "in_person": bucket.in_person,
            "mail_in": bucket.mail_in,
        }
        for name, bucket in sorted(rollup.by_county.items())
    ]
    body: dict[str, object] = {
        "source_election_id": election_id,
        "payload_kind": "statewide_roster",
        "statewide": {
            "total": rollup.in_person + rollup.mail_in,
            "in_person": rollup.in_person,
            "mail_in": rollup.mail_in,
        },
        "by_county": by_county,
    }
    if ev_date is not None:
        body["ev_date"] = ev_date
    return body


async def aggregate_roster_from_d1(
    db: D1Database,
    source_election_id: str,
    file_id: str,
    *,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    """Scan roster chunks; SQL aggregates each batch of county/method pairs."""
    election_id = require_election_id(source_election_id)
    rollup = _RosterRollup()
    carry = ""
    header_line: str | None = None
    for index in await _chunk_indices(db, election_id, file_id):
        piece_b64 = await _chunk_b64_at(db, election_id, file_id, index)
        if piece_b64 is None:
            return None
        try:
            piece_text = base64.b64decode(piece_b64).decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        carry, lines = _lines_from_carry(carry, piece_text)
        batch: list[dict[str, str]] = []
        for line in lines:
            if header_line is None:
                if "COUNTY" in line.upper() and "VOTING_METHOD" in line.upper():
                    header_line = line
                continue
            parsed = _parse_roster_line(line, header_line)
            if parsed is None:
                continue
            county, method = parsed
            batch.append({"county": county, "method": method})
            if len(batch) >= 500:
                sql_rows = await _roster_sql_batch(db, batch)
                _merge_roster_sql_rows(rollup, sql_rows)
                batch = []
        if batch:
            sql_rows = await _roster_sql_batch(db, batch)
            _merge_roster_sql_rows(rollup, sql_rows)
    if carry.strip() and header_line is not None:
        parsed = _parse_roster_line(carry, header_line)
        if parsed is not None:
            county, method = parsed
            sql_rows = await _roster_sql_batch(db, [{"county": county, "method": method}])
            _merge_roster_sql_rows(rollup, sql_rows)
    return _roster_body_from_rollup(rollup, source_election_id=election_id, ev_date=ev_date)


async def aggregate_body_from_d1(
    db: D1Database,
    source_election_id: str,
    file_id: str,
    *,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    election_id = require_election_id(source_election_id)
    kind = await detect_payload_kind_from_d1(db, election_id, file_id)
    if kind == "turnout":
        return await aggregate_turnout_from_d1(
            db, election_id, file_id, ev_date=ev_date
        )
    return await aggregate_roster_from_d1(db, election_id, file_id, ev_date=ev_date)


async def aggregate_for_published(
    db: D1Database,
    source_election_id: str,
    *,
    kv_sha256: str,
    file_id: str | None = None,
    expected_sha256: str | None = None,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    election_id = require_election_id(source_election_id)
    published = await _published_file(
        db,
        election_id,
        kv_sha256=kv_sha256,
        file_id=file_id,
        expected_sha256=expected_sha256,
    )
    if published is None:
        return None
    return await aggregate_body_from_d1(
        db,
        election_id,
        published.file_id,
        ev_date=ev_date,
    )


async def _rollup_days(
    db: D1Database,
    election_id: str,
    *,
    kv_sha256: str,
) -> list[tuple[str, dict[str, object]]]:
    days = await load_day_rows(db, election_id)
    rolled: list[tuple[str, dict[str, object]]] = []
    for day in days:
        body = await aggregate_for_published(
            db,
            election_id,
            kv_sha256=kv_sha256,
            file_id=day.file_id,
            expected_sha256=day.sha256,
            ev_date=day.ev_date,
        )
        if body is not None:
            rolled.append((day.ev_date, body))
    return rolled


def _slice_totals_resource(
    totals: dict[str, object],
    resource: str,
) -> dict[str, object] | None:
    if resource == "totals":
        return totals
    if resource == "county":
        return {
            "source_election_id": totals.get("source_election_id"),
            "ev_date": totals.get("ev_date"),
            "payload_kind": totals.get("payload_kind"),
            "by_county": totals.get("by_county"),
        }
    if resource == "by-method":
        return slice_by_method(totals)
    return None


async def build_aggregate_resource(
    db: D1Database,
    source_election_id: str,
    resource: str,
    *,
    kv_sha256: str,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    election_id = require_election_id(source_election_id)
    if resource == "by-day":
        rolled = await _rollup_days(db, election_id, kv_sha256=kv_sha256)
        if not rolled:
            totals = await aggregate_for_published(
                db, election_id, kv_sha256=kv_sha256
            )
            if totals is None:
                return None
            rolled = [("latest", totals)]
        return build_by_day_series(source_election_id=election_id, days=tuple(rolled))

    if resource == "county-by-day":
        rolled = await _rollup_days(db, election_id, kv_sha256=kv_sha256)
        if not rolled:
            return None
        return build_county_by_day_series(source_election_id=election_id, days=tuple(rolled))

    file_id = None
    expected_sha256 = None
    resolved_ev_date = ev_date
    if ev_date is not None:
        day = await resolve_day_for_date(db, election_id, ev_date)
        if day is None:
            return None
        file_id = day.file_id
        expected_sha256 = day.sha256
        resolved_ev_date = day.ev_date

    totals = await aggregate_for_published(
        db,
        election_id,
        kv_sha256=kv_sha256,
        file_id=file_id,
        expected_sha256=expected_sha256,
        ev_date=resolved_ev_date,
    )
    if totals is None:
        return None
    return _slice_totals_resource(totals, resource)


def explain_county_by_day_plan() -> str:
    """SQL shape used for each day file in county-by-day (for EXPLAIN in PR notes)."""
    return (
        "Per ev_date: resolve early_vote_day.file_id; "
        f"roster uses {_ROSTER_BATCH_SQL[:80]}... per chunk batch; "
        f"turnout uses {_TURNOUT_COUNTY_SQL[:80]}..."
    )

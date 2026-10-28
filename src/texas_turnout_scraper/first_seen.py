"""Distinct-voter counts for the aggregate API: each live voter counted once, on first appearance.

The portal re-lists voters on later days (ADR-012: day 11 re-listed 656,734 voters from earlier
days), so summing day files counts them twice. ``early_vote_first_seen`` holds, per
``(ev_date, county, voting_method)``, the number of voters whose earliest live roster row is at
those coordinates. Summed over every day it is ``COUNT(DISTINCT id_voter)``; for one day it is
that day's new voters, and the day's rows minus that are its restated rows.

The build (``first_seen_build``) writes the table and a signature over
``early_vote_roster_version``. The Worker publishes the counts only while that signature matches
the live versions; otherwise the fields are null and ``dedupe.status`` says why. A voter listed
twice on their first day is attributed to one of those rows, whichever SQLite's ``MIN`` picks.

Imported by the Worker: standard library and Worker-safe modules only. Nothing here reads or
logs a VUID; the SQL returns counts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from texas_turnout_scraper.early_vote_aggregates import _normalize_method
from texas_turnout_scraper.early_vote_d1_read import D1Database, _d1_fetch_all, _d1_int, _row_value
from texas_turnout_scraper.tx_counties import county_fips

# Part of the signature, so a change to how counts are derived rebuilds them on the next run.
FIRST_SEEN_VERSION = 1

TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_first_seen (ev_date TEXT NOT NULL, "
    "county TEXT NOT NULL, voting_method TEXT NOT NULL, n INTEGER NOT NULL, "
    "PRIMARY KEY (ev_date, county, voting_method))"
)
META_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_first_seen_meta (name TEXT NOT NULL, "
    "value TEXT NOT NULL, PRIMARY KEY (name))"
)
SCHEMA_CREATES: tuple[str, ...] = (TABLE_SQL, META_TABLE_SQL)
TABLES_PRESENT_SQL = (
    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN "
    "('early_vote_first_seen', 'early_vote_first_seen_meta')"
)
_TABLE_COUNT = 2

VERSIONS_SQL = (
    "SELECT ev_date, version, sha256, row_count FROM early_vote_roster_version "
    "WHERE source_election_id = ? ORDER BY ev_date"
)
SIGNATURE_SQL = "SELECT value FROM early_vote_first_seen_meta WHERE name = 'signature'"
COUNTS_SQL = "SELECT ev_date, county, voting_method, n FROM early_vote_first_seen"

# One query over every live row takes about 15 s on the 4.3M-row roster, near D1's limit, so the
# build splits voters by the last character of the VUID. A voter's rows all fall in one part.
# One tenth took 2.5 s on 2026-10-27. The last part catches any VUID not ending in a digit.
_DIGITS = tuple(str(d) for d in range(10))
_PARTITION_SQL = (
    "SELECT d, upper(trim(county)) AS county, upper(trim(voting_method)) AS method, "
    "COUNT(*) AS n FROM (SELECT MIN(day) AS d, county, voting_method "
    "FROM early_vote_roster_row WHERE removed_in IS NULL AND {where} GROUP BY id_voter) "
    "GROUP BY 1, 2, 3"
)
PARTITIONS: tuple[tuple[str, list[str]], ...] = (
    *((_PARTITION_SQL.format(where="substr(id_voter, -1) = ?"), [d]) for d in _DIGITS),
    (
        _PARTITION_SQL.format(
            where=f"substr(id_voter, -1) NOT IN ({', '.join(repr(d) for d in _DIGITS)})"
        ),
        [],
    ),
)

_DELETE_META_SQL = "DELETE FROM early_vote_first_seen_meta"
_DELETE_COUNTS_SQL = "DELETE FROM early_vote_first_seen"
_INSERT_COUNTS_SQL = (
    "INSERT INTO early_vote_first_seen (ev_date, county, voting_method, n) "
    "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
    "json_extract(value, '$[2]'), CAST(json_extract(value, '$[3]') AS INTEGER) "
    "FROM json_each(?)"
)
_INSERT_META_SQL = "INSERT INTO early_vote_first_seen_meta (name, value) VALUES (?, ?)"
# D1 binds at most 2 MB per string; a row is about 40 bytes.
_ROWS_PER_INSERT = 5_000

Method = Literal["in_person", "mail_in"]
Coord = tuple[str, str, str]
Status = Literal["current", "stale", "missing", "not_applicable"]


def day_iso(day: object) -> str | None:
    """ISO date for a roster ``day`` (a proleptic ordinal, see ``early_vote_roster_rows``)."""
    number = _d1_int(day)
    return None if number is None else dt.date.fromordinal(number).isoformat()


def signature(version_rows: Iterable[object]) -> str:
    """sha256 over the roster versions the counts were built from."""
    key = [
        [
            str(_row_value(row, "ev_date")),
            _d1_int(_row_value(row, "version")),
            str(_row_value(row, "sha256")),
            _d1_int(_row_value(row, "row_count")),
        ]
        for row in version_rows
    ]
    key.sort()
    payload = json.dumps([FIRST_SEEN_VERSION, key], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def add_partition_rows(counts: dict[Coord, int], rows: Iterable[object]) -> int:
    """Fold one partition's rows into ``counts``; returns voters whose method is unrecognized."""
    unknown = 0
    for row in rows:
        ev_date = day_iso(_row_value(row, "d"))
        n = _d1_int(_row_value(row, "n")) or 0
        method = _normalize_method(str(_row_value(row, "method") or ""))
        if ev_date is None or method is None:
            unknown += n
            continue
        coord = (ev_date, str(_row_value(row, "county") or ""), method)
        counts[coord] = counts.get(coord, 0) + n
    return unknown


def replace_statements(counts: Mapping[Coord, int], sig: str) -> list[tuple[str, list[str]]]:
    """One batch: drop the signature first, replace the counts, write the signature last.

    A reader between statements sees no signature and treats the counts as missing.
    """
    rows = [[*coord, n] for coord, n in sorted(counts.items())]
    inserts = [
        (_INSERT_COUNTS_SQL, [json.dumps(rows[i : i + _ROWS_PER_INSERT], separators=(",", ":"))])
        for i in range(0, len(rows), _ROWS_PER_INSERT)
    ]
    return [
        *((sql, []) for sql in SCHEMA_CREATES),
        (_DELETE_META_SQL, []),
        (_DELETE_COUNTS_SQL, []),
        *inserts,
        (_INSERT_META_SQL, ["distinct_voters", str(sum(counts.values()))]),
        (_INSERT_META_SQL, ["signature", sig]),
    ]


@dataclass(frozen=True)
class FirstSeen:
    status: Status
    counts: Mapping[Coord, int] = field(default_factory=dict)

    @property
    def current(self) -> bool:
        return self.status == "current"

    def totals(self, days: set[str], county: str | None = None) -> tuple[int, int]:
        """(in_person, mail_in) first appearances on ``days``, statewide or in one county."""
        in_person = mail_in = 0
        for (ev_date, where, method), n in self.counts.items():
            if ev_date not in days or (county is not None and where != county):
                continue
            if method == "in_person":
                in_person += n
            else:
                mail_in += n
        return in_person, mail_in


async def load_first_seen(db: D1Database, election_id: str) -> FirstSeen:
    present = await _d1_fetch_all(db.prepare(TABLES_PRESENT_SQL))
    if not present or _d1_int(_row_value(present[0], "n")) != _TABLE_COUNT:
        return FirstSeen("missing")
    stored = await _d1_fetch_all(db.prepare(SIGNATURE_SQL))
    if not stored:
        return FirstSeen("missing")
    versions = await _d1_fetch_all(db.prepare(VERSIONS_SQL).bind(election_id))
    if _row_value(stored[0], "value") != signature(versions):
        return FirstSeen("stale")
    counts: dict[Coord, int] = {}
    for row in await _d1_fetch_all(db.prepare(COUNTS_SQL)):
        coord = (
            str(_row_value(row, "ev_date")),
            str(_row_value(row, "county")),
            str(_row_value(row, "voting_method")),
        )
        counts[coord] = _d1_int(_row_value(row, "n")) or 0
    return FirstSeen("current", counts)


def _row_total(obj: Mapping[str, object]) -> int | None:
    """The object's row count: ``total``, or ``in_person + mail_in`` where there is no total."""
    if "total" in obj:
        return _d1_int(obj["total"])
    in_person, mail_in = _d1_int(obj.get("in_person")), _d1_int(obj.get("mail_in"))
    return None if in_person is None or mail_in is None else in_person + mail_in


def _add_counts(obj: dict[str, object], prefix: str, found: tuple[int, int] | None) -> None:
    """Add ``rows_reported`` and ``{prefix}_voters`` / ``_in_person`` / ``_mail_in``."""
    in_person: int | None = None
    mail_in: int | None = None
    voters: int | None = None
    if found is not None:
        in_person, mail_in = found
        voters = in_person + mail_in
    rows = _row_total(obj)
    obj["rows_reported"] = rows
    obj[f"{prefix}_voters"] = voters
    if prefix == "new":
        obj["restated_rows"] = None if voters is None or rows is None else max(rows - voters, 0)
    if "in_person" in obj:
        obj[f"{prefix}_in_person"] = in_person
    if "mail_in" in obj:
        obj[f"{prefix}_mail_in"] = mail_in


def _annotate_counts(
    container: dict[str, object], first_seen: FirstSeen, days: set[str], prefix: str
) -> None:
    statewide = container.get("statewide")
    if isinstance(statewide, dict):
        _add_counts(statewide, prefix, first_seen.totals(days) if first_seen.current else None)
    by_county = container.get("by_county")
    for row in by_county if isinstance(by_county, list) else []:
        if isinstance(row, dict):
            found = first_seen.totals(days, str(row.get("county"))) if first_seen.current else None
            _add_counts(row, prefix, found)


def _is_iso_date(value: object) -> bool:
    try:
        dt.date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def _annotate_by_day(body: dict[str, object], first_seen: FirstSeen) -> None:
    running: int | None = 0
    series = body.get("series")
    for entry in series if isinstance(series, list) else []:
        if not isinstance(entry, dict):
            continue
        day = {str(entry.get("ev_date"))}
        if "by_county" in entry:
            _annotate_counts(entry, first_seen, day, "new")
            continue
        _add_counts(entry, "new", first_seen.totals(day) if first_seen.current else None)
        restated = entry.get("restated_rows")
        entry["restated"] = restated > 0 if isinstance(restated, int) else None
        new = entry.get("new_voters")
        running = running + new if running is not None and isinstance(new, int) else None
        entry["cumulative_unique_voters"] = running


def _matchable(body: Mapping[str, object], resource: str, ev_date: str | None) -> bool:
    """False for a body whose days cannot be matched to first appearances.

    A cumulative body built without a ``days`` list, or a series whose only entry is the
    ``"latest"`` placeholder, falls back to the published file and names no stored day.
    """
    if resource in ("by-day", "county-by-day"):
        series = body.get("series")
        entries = series if isinstance(series, list) else []
        return all(isinstance(e, dict) and _is_iso_date(e.get("ev_date")) for e in entries)
    return ev_date is not None or isinstance(body.get("days"), list)


def _county_rows(body: dict[str, object]) -> Iterable[dict[str, object]]:
    lists = [body.get("by_county")]
    series = body.get("series")
    if isinstance(series, list):
        lists += [entry.get("by_county") for entry in series if isinstance(entry, dict)]
    for rows in lists:
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                yield row


def annotate(
    body: dict[str, object], resource: str, first_seen: FirstSeen, *, ev_date: str | None
) -> dict[str, object]:
    """Add distinct-voter counts, restated-day marks and ``county_fips`` to an aggregate body."""
    unmatched = 0
    for row in _county_rows(body):
        row["county_fips"] = county_fips(str(row.get("county") or ""))
        unmatched += row["county_fips"] is None
    body["county_fips_unmatched"] = unmatched

    if body.get("payload_kind") == "turnout":
        body["dedupe"] = {"status": "not_applicable"}
        return body
    if first_seen.current and not _matchable(body, resource, ev_date):
        first_seen = FirstSeen("missing")
    body["dedupe"] = {"status": first_seen.status, "basis": "first_appearance"}
    if resource in ("by-day", "county-by-day"):
        _annotate_by_day(body, first_seen)
    elif ev_date is not None:
        _annotate_counts(body, first_seen, {ev_date}, "new")
    else:
        days = body.get("days")
        listed = {str(d) for d in days} if isinstance(days, list) else set()
        _annotate_counts(body, first_seen, listed, "unique")
    return body


def cacheable(body: Mapping[str, object]) -> bool:
    """False while the distinct counts are stale or missing: such a body must not be kept."""
    dedupe = body.get("dedupe")
    if not isinstance(dedupe, Mapping):
        return True
    return dedupe.get("status") in ("current", "not_applicable")

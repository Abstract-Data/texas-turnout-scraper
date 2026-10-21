"""Worker aggregate reads from loader-precomputed D1 rows only.

On a published file with no precomputed rows, raises
:class:`AggregatesNotComputed` so the API can fail fast (503) instead of
walking chunk payloads in the isolate.
"""

from __future__ import annotations

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_aggregates import (
    build_by_day_series,
    build_county_by_day_series,
    slice_by_method,
)
from texas_turnout_scraper.early_vote_d1_precompute import load_precomputed_body
from texas_turnout_scraper.early_vote_d1_read import (
    D1Database,
    load_day_rows,
    load_file_views,
    load_pointer_file_id,
    resolve_day_for_date,
)
from texas_turnout_scraper.early_vote_sync import EarlyVoteFileView, choose_published_file


class AggregatesNotComputed(Exception):
    """Published file exists but loader has not written aggregate rows yet."""


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


async def _precomputed_for_published(
    db: D1Database,
    source_election_id: str,
    *,
    kv_sha256: str,
    file_id: str | None = None,
    expected_sha256: str | None = None,
    ev_date: str | None = None,
) -> dict[str, object]:
    election_id = require_election_id(source_election_id)
    published = await _published_file(
        db,
        election_id,
        kv_sha256=kv_sha256,
        file_id=file_id,
        expected_sha256=expected_sha256,
    )
    if published is None:
        msg = "published file not ready"
        raise AggregatesNotComputed(msg)
    body = await load_precomputed_body(
        db,
        election_id,
        published.file_id,
        ev_date=ev_date,
    )
    if body is None:
        msg = "precomputed aggregate rows missing"
        raise AggregatesNotComputed(msg)
    return body


async def _rollup_days(
    db: D1Database,
    election_id: str,
    *,
    kv_sha256: str,
) -> list[tuple[str, dict[str, object]]]:
    days = await load_day_rows(db, election_id)
    rolled: list[tuple[str, dict[str, object]]] = []
    for day in days:
        body = await _precomputed_for_published(
            db,
            election_id,
            kv_sha256=kv_sha256,
            file_id=day.file_id,
            expected_sha256=day.sha256,
            ev_date=day.ev_date,
        )
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


async def _build_by_day_resource(
    db: D1Database,
    election_id: str,
    *,
    kv_sha256: str,
) -> dict[str, object]:
    rolled = await _rollup_days(db, election_id, kv_sha256=kv_sha256)
    if not rolled:
        totals = await _precomputed_for_published(db, election_id, kv_sha256=kv_sha256)
        rolled = [("latest", totals)]
    return build_by_day_series(source_election_id=election_id, days=tuple(rolled))


async def _build_county_by_day_resource(
    db: D1Database,
    election_id: str,
    *,
    kv_sha256: str,
) -> dict[str, object] | None:
    rolled = await _rollup_days(db, election_id, kv_sha256=kv_sha256)
    if not rolled:
        return None
    return build_county_by_day_series(source_election_id=election_id, days=tuple(rolled))


async def _totals_for_ev_date(
    db: D1Database,
    election_id: str,
    *,
    kv_sha256: str,
    ev_date: str | None,
) -> dict[str, object]:
    file_id = None
    expected_sha256 = None
    resolved_ev_date = ev_date
    if ev_date is not None:
        day = await resolve_day_for_date(db, election_id, ev_date)
        if day is None:
            msg = "unknown ev_date"
            raise AggregatesNotComputed(msg)
        file_id = day.file_id
        expected_sha256 = day.sha256
        resolved_ev_date = day.ev_date
    return await _precomputed_for_published(
        db,
        election_id,
        kv_sha256=kv_sha256,
        file_id=file_id,
        expected_sha256=expected_sha256,
        ev_date=resolved_ev_date,
    )


async def build_precomputed_aggregate_resource(
    db: D1Database,
    source_election_id: str,
    resource: str,
    *,
    kv_sha256: str,
    ev_date: str | None = None,
) -> dict[str, object] | None:
    election_id = require_election_id(source_election_id)
    if resource == "by-day":
        return await _build_by_day_resource(db, election_id, kv_sha256=kv_sha256)
    if resource == "county-by-day":
        return await _build_county_by_day_resource(db, election_id, kv_sha256=kv_sha256)
    totals = await _totals_for_ev_date(db, election_id, kv_sha256=kv_sha256, ev_date=ev_date)
    sliced = _slice_totals_resource(totals, resource)
    if sliced is None:
        msg = f"unknown aggregate resource {resource}"
        raise AggregatesNotComputed(msg)
    return sliced

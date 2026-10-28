"""Precinct points and bounding boxes for one election (public geography, no voter data).

Joins the loaded ``geo_precinct_alias`` rows (roster county and precinct text to a state
precinct) to ``geo_area`` so a consumer can attach a point and a box to a voter by joining on
county and precinct. Cached in KV per election version through ``read_resource``, the same
path the aggregate routes use. See docs/adr/010-geography-tables.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ev_api.read import ReadOutcome, ResourceQuery, read_resource, response_headers
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.civix.read_client import version_from_etag
from texas_turnout_scraper.early_vote_d1_precompute_read import AggregatesNotComputed
from texas_turnout_scraper.early_vote_d1_read import D1Database, _d1_fetch_all, _d1_int, _row_value
from texas_turnout_scraper.geo_tables import AREA_COLUMNS_SQL, BBOX_COLUMNS

logger = logging.getLogger(__name__)

SEGMENTS = ("geo", "precincts")
GEO_NOT_LOADED_DETAIL = "geo_not_loaded"

_ALIAS_COLUMNS_SQL = "SELECT name FROM pragma_table_info('geo_precinct_alias')"
_ALIAS_TOTAL_SQL = "SELECT COUNT(*) AS n FROM geo_precinct_alias"
# One row with one JSON text cell keeps D1 result conversion constant-size for ~10k precincts.
_MATCHED_SQL = (
    "SELECT json_group_array(json_array(a.county, a.precinct, a.pctkey, g.lat, g.lon, "
    "g.min_lon, g.min_lat, g.max_lon, g.max_lat)) AS found "
    "FROM geo_precinct_alias a JOIN geo_area g ON g.kind = 'precinct' AND g.code = a.pctkey "
    "WHERE a.pctkey IS NOT NULL"
)
_AREA_REQUIRED = frozenset({"kind", "code", "name", "lat", "lon", *BBOX_COLUMNS})
_ALIAS_REQUIRED = frozenset({"county", "precinct", "pctkey", "rule"})


async def _column_names(db: D1Database, sql: str) -> set[str]:
    names = (_row_value(row, "name") for row in await _d1_fetch_all(db.prepare(sql)))
    return {name for name in names if isinstance(name, str)}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _precinct_entry(item: object) -> dict[str, object] | None:
    if not isinstance(item, list) or len(item) != 9:
        return None
    county, precinct, pctkey, lat, lon, min_lon, min_lat, max_lon, max_lat = item
    if not all(isinstance(v, str) for v in (county, precinct, pctkey)):
        return None
    edges = [_number(v) for v in (min_lon, min_lat, max_lon, max_lat)]
    return {
        "county": county,
        "precinct": precinct,
        "pctkey": pctkey,
        "lat": _number(lat),
        "lon": _number(lon),
        "bbox": None if any(v is None for v in edges) else edges,
    }


class GeoPrecinctStore:
    """Builds the precinct body from one election's D1; ``AggregateStore`` shaped for read.py."""

    def __init__(self, db: D1Database) -> None:
        self._db = db
        self.geo_missing = False

    async def build_body(
        self,
        source_election_id: str,
        segments: tuple[str, ...],
        *,
        kv_sha256: str,
    ) -> dict[str, object] | None:
        if segments != SEGMENTS:
            return None
        election_id = require_election_id(source_election_id)
        area_columns = await _column_names(self._db, AREA_COLUMNS_SQL)
        alias_columns = await _column_names(self._db, _ALIAS_COLUMNS_SQL)
        if not (_AREA_REQUIRED <= area_columns and _ALIAS_REQUIRED <= alias_columns):
            self.geo_missing = True
            raise AggregatesNotComputed
        total_rows = await _d1_fetch_all(self._db.prepare(_ALIAS_TOTAL_SQL))
        total = _d1_int(_row_value(total_rows[0], "n")) if total_rows else 0
        rows = await _d1_fetch_all(self._db.prepare(_MATCHED_SQL))
        found = _row_value(rows[0], "found") if rows else None
        parsed = json.loads(found) if isinstance(found, str) else []
        entries = [e for e in map(_precinct_entry, parsed) if e is not None]
        entries.sort(key=lambda e: (str(e["county"]), str(e["precinct"])))
        return {
            "source_election_id": election_id,
            "count": len(entries),
            "unmatched": max((total or 0) - len(entries), 0),
            "precincts": entries,
        }


def _respond(outcome: ReadOutcome, store: GeoPrecinctStore) -> Response:
    headers = response_headers(outcome)
    if outcome.status_code == 304:
        return Response(status_code=304, headers=headers)
    if outcome.status_code == 200 and outcome.body is not None and outcome.etag is not None:
        version = str(version_from_etag(outcome.etag))
        return JSONResponse({**outcome.body, "version": version}, headers=headers)
    if outcome.status_code == 404:
        return JSONResponse({"detail": "not_found"}, status_code=404, headers=headers)
    if outcome.status_code == 400:
        return JSONResponse({"detail": "bad_request"}, status_code=400, headers=headers)
    if outcome.status_code == 503 and store.geo_missing:
        return JSONResponse({"detail": GEO_NOT_LOADED_DETAIL}, status_code=503)
    if outcome.status_code == 503 and outcome.body is not None:
        return JSONResponse(outcome.body, status_code=503, headers=headers)
    return Response(status_code=outcome.status_code, headers=headers)


class _GeoApi:
    def __init__(self, deps_for_request: Callable[[Request], Any]) -> None:
        self._deps = deps_for_request

    async def geo_precincts(self, source_election_id: str, request: Request) -> Response:
        active = self._deps(request)
        try:
            election_id = require_election_id(source_election_id)
        except ValueError:
            return JSONResponse({"detail": "bad_request"}, status_code=400)
        db = active.rosters.database(election_id)
        if db is None:
            return JSONResponse({"detail": "not_found"}, status_code=404)
        store = GeoPrecinctStore(db)
        outcome = await read_resource(
            ResourceQuery(
                source_election_id=election_id,
                segments=SEGMENTS,
                if_none_match=request.headers.get("if-none-match"),
            ),
            active.kv,
            store,
            ttl_seconds=active.ttl_seconds,
        )
        return _respond(outcome, store)


def register_geo_routes(app: FastAPI, deps_for_request: Callable[[Request], Any]) -> None:
    api = _GeoApi(deps_for_request)
    app.add_api_route(
        "/elections/{source_election_id}/geo/precincts",
        api.geo_precincts,
        methods=["GET"],
        response_model=None,
    )

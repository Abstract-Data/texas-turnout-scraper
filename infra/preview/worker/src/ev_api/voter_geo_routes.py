"""Per-voter geography for one roster day (VUIDs and precincts, no names), roster scope only.

``GET /elections/{id}/roster/{ev_date}/geo?page=k`` serves the ``k``th of ``VOTER_GEO_PAGES``
row-hash ranges of the day's live roster rows, joined to ``voter_geo`` and, where a voter has a
residential point, ``voter_geo_point``. One D1 query returns the page as one JSON text cell and the
Worker wraps it without parsing, so CPU per request is constant. See
docs/adr/015-voter-geography.md.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from ev_api.roster_routes import _EV_DATE_RE, _etag_matches, _quoted, _RosterApi
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_read import D1Database, _d1_fetch_all, _d1_int, _row_value
from texas_turnout_scraper.early_vote_d1_roster import day_etag, find_day
from texas_turnout_scraper.early_vote_roster_rows import day_number, key_ranges
from texas_turnout_scraper.voter_geo_tables import (
    VOTER_GEO_PAGE_SQL,
    VOTER_GEO_PAGES,
    VOTER_GEO_PRESENT_SQL,
    VOTER_GEO_SIGNATURE_SQL,
    VOTER_GEO_TABLE_COUNT,
)

logger = logging.getLogger(__name__)

VOTER_GEO_NOT_LOADED_DETAIL = "voter_geo_not_loaded"
_PAGE_RE = re.compile(r"[0-9]{1,3}")
_MEDIA_TYPE = "application/json"
# VUIDs with precincts: shared caches must not keep a copy; the holder's client may revalidate.
_CACHE_CONTROL = "private, no-cache"
_SIGNATURE_IN_TAG = 16


def _page_number(text: str | None) -> int | None:
    if text is None or _PAGE_RE.fullmatch(text) is None:
        return None
    page = int(text)
    return page if page < VOTER_GEO_PAGES else None


async def _signatures(db: D1Database) -> tuple[str, str] | None:
    """The last build's signature and the last point apply's ("" before the first), or None.

    None means the tables or the Stage 1 signature are missing. The point signature is part of the
    ETag, since a point apply changes served rows without touching the Stage 1 signature.
    """
    present = await _d1_fetch_all(db.prepare(VOTER_GEO_PRESENT_SQL))
    if not present or _d1_int(_row_value(present[0], "n")) != VOTER_GEO_TABLE_COUNT:
        return None
    rows = await _d1_fetch_all(db.prepare(VOTER_GEO_SIGNATURE_SQL))
    if not rows:
        return None
    signature = _row_value(rows[0], "signature")
    point_signature = _row_value(rows[0], "point_signature")
    if not isinstance(signature, str) or not signature:
        return None
    return signature, point_signature if isinstance(point_signature, str) else ""


class _VoterGeoApi(_RosterApi):
    async def roster_geo(
        self,
        source_election_id: str,
        ev_date: str,
        request: Request,
        page: str | None = Query(default=None),
    ) -> Response:
        denied, db = await self._roster_gate(request, source_election_id)
        if denied is not None:
            return denied
        number = _page_number(page)
        if number is None or _EV_DATE_RE.fullmatch(ev_date) is None:
            return JSONResponse({"detail": "bad_request"}, status_code=400)
        try:
            day_key = str(day_number(ev_date))
        except ValueError:
            return JSONResponse({"detail": "bad_request"}, status_code=400)
        election_id = require_election_id(source_election_id)
        day = await find_day(db, election_id, ev_date)
        if day is None:
            return JSONResponse({"detail": "not_found"}, status_code=404)
        signatures = await _signatures(db)
        if signatures is None:
            return JSONResponse({"detail": VOTER_GEO_NOT_LOADED_DETAIL}, status_code=503)
        signature, point_signature = signatures
        point_part = f".{point_signature[:_SIGNATURE_IN_TAG]}" if point_signature else ""
        tag = f"{day_etag(day)}.{signature[:_SIGNATURE_IN_TAG]}{point_part}.p{number}"
        headers = {"ETag": _quoted(tag), "Cache-Control": _CACHE_CONTROL}
        if _etag_matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers=headers)
        started = time.perf_counter()
        low, high = key_ranges(VOTER_GEO_PAGES)[number]
        rows = await _d1_fetch_all(db.prepare(VOTER_GEO_PAGE_SQL).bind(day_key, low, high))
        cell = _row_value(rows[0], "voters") if rows else None
        if not isinstance(cell, str):
            raise RuntimeError("voter_geo page query returned no JSON cell")
        count = _d1_int(_row_value(rows[0], "n")) or 0
        head = json.dumps(
            {
                "source_election_id": election_id,
                "ev_date": ev_date,
                "page": number,
                "pages": VOTER_GEO_PAGES,
                "count": count,
            }
        )
        logger.info(
            "voter_geo page election_id=%s page=%s count=%s ms=%s",
            election_id,
            number,
            count,
            int((time.perf_counter() - started) * 1000),
        )
        # The cell is already JSON: splice it in instead of parsing and re-serializing it.
        body = f'{head[:-1]},"voters":{cell}}}'
        return Response(content=body, media_type=_MEDIA_TYPE, headers=headers)


def register_voter_geo_routes(app: FastAPI, deps_for_request: Callable[[Request], Any]) -> None:
    api = _VoterGeoApi(deps_for_request)
    app.add_api_route(
        "/elections/{source_election_id}/roster/{ev_date}/geo",
        api.roster_geo,
        methods=["GET"],
        response_model=None,
    )

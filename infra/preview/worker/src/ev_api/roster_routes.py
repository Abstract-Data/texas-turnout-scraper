"""Elections list and roster routes (names and VUIDs) for the preview read API."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ev_api.auth import consumer_token_scope, read_request_authorized
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.consumer_tokens import SCOPE_ROSTER
from texas_turnout_scraper.early_vote_d1_roster import (
    RosterDay,
    day_etag,
    find_day,
    list_days,
    roster_etag,
    stream_all_days,
    stream_day,
)

logger = logging.getLogger(__name__)

_EV_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_ROSTER_MEDIA_TYPE = "text/csv; charset=utf-8"
# Names and VUIDs: shared caches must not keep a copy; the holder's client may revalidate.
_ROSTER_CACHE_CONTROL = "private, no-cache"


def _quoted(tag: str) -> str:
    return f'"{tag}"'


def _etag_matches(if_none_match: str | None, tag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        value = candidate.strip().removeprefix("W/").strip('"')
        if value == "*" or value == tag:
            return True
    return False


def _election_summary(election_id: str, days: tuple[RosterDay, ...]) -> dict[str, object]:
    return {
        "source_election_id": election_id,
        "first_ev_date": days[0].ev_date if days else None,
        "last_ev_date": days[-1].ev_date if days else None,
        "total_rows": sum(day.rows or 0 for day in days),
        "days": [
            {
                "ev_date": day.ev_date,
                "rows": day.rows,
                "sha256": day.sha256,
                "byte_length": day.byte_length,
            }
            for day in days
        ],
    }


async def _logged_stream(source: AsyncIterator[bytes], route: str) -> AsyncIterator[bytes]:
    try:
        async for piece in source:
            yield piece
    except Exception as exc:
        logger.error("roster stream failed %s route=%s", type(exc).__name__, route)
        raise


class _RosterApi:
    """Route handlers; ``deps_for_request`` returns the request's ``ReadDeps``."""

    def __init__(self, deps_for_request: Callable[[Request], Any]) -> None:
        self._deps = deps_for_request

    async def _read_allowed(self, request: Request) -> bool:
        active = self._deps(request)
        return await read_request_authorized(
            request.headers,
            access_client_id=active.access_client_id,
            access_client_secret=active.access_client_secret,
            kv=active.kv,
            token_db=active.token_db,
        )

    async def _roster_gate(
        self, request: Request, source_election_id: str
    ) -> tuple[Response | None, Any]:
        active = self._deps(request)
        scope = await consumer_token_scope(request.headers, kv=active.kv, token_db=active.token_db)
        if scope is None:
            return Response(status_code=401), None
        if scope != SCOPE_ROSTER:
            return JSONResponse({"detail": "forbidden"}, status_code=403), None
        try:
            election_id = require_election_id(source_election_id)
        except ValueError:
            return JSONResponse({"detail": "bad_request"}, status_code=400), None
        db = active.rosters.database(election_id)
        if db is None:
            return JSONResponse({"detail": "not_found"}, status_code=404), None
        return None, db

    @staticmethod
    def _stream_response(
        stream: AsyncIterator[bytes],
        *,
        tag: str,
        filename: str,
        route: str,
        length: int | None,
    ) -> Response:
        headers = {
            "ETag": _quoted(tag),
            "Cache-Control": _ROSTER_CACHE_CONTROL,
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if length is not None:
            headers["Content-Length"] = str(length)
        return StreamingResponse(
            _logged_stream(stream, route), media_type=_ROSTER_MEDIA_TYPE, headers=headers
        )

    async def elections_index(self, request: Request, q: str | None = Query(default=None)):
        if not await self._read_allowed(request):
            return Response(status_code=401)
        active = self._deps(request)
        needle = (q or "").strip().lower()
        elections = []
        for election_id in active.rosters.election_ids():
            db = active.rosters.database(election_id)
            if db is None or (needle and needle not in election_id.lower()):
                continue
            elections.append(_election_summary(election_id, await list_days(db, election_id)))
        return JSONResponse(
            {"elections": elections}, headers={"Cache-Control": "private, no-cache"}
        )

    async def election_detail(self, source_election_id: str, request: Request):
        if not await self._read_allowed(request):
            return Response(status_code=401)
        try:
            election_id = require_election_id(source_election_id)
        except ValueError:
            return JSONResponse({"detail": "bad_request"}, status_code=400)
        db = self._deps(request).rosters.database(election_id)
        if db is None:
            return JSONResponse({"detail": "not_found"}, status_code=404)
        return JSONResponse(_election_summary(election_id, await list_days(db, election_id)))

    async def roster_all_days(self, source_election_id: str, request: Request):
        denied, db = await self._roster_gate(request, source_election_id)
        if denied is not None:
            return denied
        election_id = require_election_id(source_election_id)
        days = await list_days(db, election_id)
        if not days:
            return JSONResponse({"detail": "not_found"}, status_code=404)
        tag = roster_etag(days)
        if _etag_matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers={"ETag": _quoted(tag)})
        return self._stream_response(
            stream_all_days(db, election_id, days),
            tag=tag,
            filename=f"roster_{election_id}_all_days.csv",
            route="roster_all_days",
            length=None,
        )

    async def roster_one_day(self, source_election_id: str, ev_date: str, request: Request):
        denied, db = await self._roster_gate(request, source_election_id)
        if denied is not None:
            return denied
        if _EV_DATE_RE.fullmatch(ev_date) is None:
            return JSONResponse({"detail": "bad_request"}, status_code=400)
        election_id = require_election_id(source_election_id)
        day = await find_day(db, election_id, ev_date)
        if day is None:
            return JSONResponse({"detail": "not_found"}, status_code=404)
        tag = day_etag(day)
        if _etag_matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers={"ETag": _quoted(tag)})
        return self._stream_response(
            stream_day(db, election_id, day),
            tag=tag,
            filename=f"roster_{election_id}_{ev_date}.csv",
            route="roster_one_day",
            length=day.byte_length,
        )


def register_roster_routes(app: FastAPI, deps_for_request: Callable[[Request], Any]) -> None:
    api = _RosterApi(deps_for_request)
    app.add_api_route("/elections", api.elections_index, methods=["GET"], response_model=None)
    app.add_api_route(
        "/elections/{source_election_id}", api.election_detail, methods=["GET"], response_model=None
    )
    app.add_api_route(
        "/elections/{source_election_id}/roster",
        api.roster_all_days,
        methods=["GET"],
        response_model=None,
    )
    app.add_api_route(
        "/elections/{source_election_id}/roster/{ev_date}",
        api.roster_one_day,
        methods=["GET"],
        response_model=None,
    )

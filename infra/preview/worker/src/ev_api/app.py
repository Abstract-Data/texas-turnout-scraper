"""FastAPI aggregate read API for the preview Worker."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from ev_api.auth import read_request_authorized
from ev_api.bindings import WorkersAggregateStore, WorkersKvNamespace, binding_map_from_env
from ev_api.read import (
    DEFAULT_KV_TTL_SECONDS,
    AggregateStore,
    KvNamespace,
    ReadOutcome,
    ResourceQuery,
    read_resource,
    response_headers,
)
from texas_turnout_scraper.civix.read_client import http_path

logger = logging.getLogger(__name__)

_EV_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class ReadDeps:
    kv: KvNamespace
    store: AggregateStore
    access_client_id: str
    access_client_secret: str
    token_db: object | None = None
    ttl_seconds: int = DEFAULT_KV_TTL_SECONDS


class _ClosedKv:
    async def get(self, key: str) -> str | None:
        return None

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None


class _ClosedStore:
    async def build_body(
        self,
        source_election_id: str,
        segments: tuple[str, ...],
        *,
        kv_sha256: str,
    ) -> dict[str, object] | None:
        return None


def deps_from_env(env: object) -> ReadDeps:
    kv_namespace = getattr(env, "EV_KV", None) or getattr(env, "EV", None)
    kv: KvNamespace = _ClosedKv() if kv_namespace is None else WorkersKvNamespace(kv_namespace)
    mapping = binding_map_from_env(getattr(env, "EV_D1_BINDINGS", ""))
    store: AggregateStore = WorkersAggregateStore(env, mapping)
    client_id = getattr(env, "ACCESS_CLIENT_ID", "") or ""
    client_secret = getattr(env, "ACCESS_CLIENT_SECRET", "") or ""
    token_db = getattr(env, "DB", None)
    return ReadDeps(
        kv=kv,
        store=store,
        access_client_id=str(client_id),
        access_client_secret=str(client_secret),
        token_db=token_db,
    )


def _deps_for_request(request: Request) -> ReadDeps:
    env = request.scope.get("env")
    if env is not None:
        return deps_from_env(env)
    return request.app.state.read_deps


def outcome_response(outcome: ReadOutcome) -> Response:
    headers = response_headers(outcome)
    if outcome.status_code == 304:
        return Response(status_code=304, headers=headers)
    if outcome.status_code == 200 and outcome.body is not None:
        return JSONResponse(outcome.body, headers=headers)
    if outcome.status_code == 404:
        return JSONResponse({"detail": "not_found"}, status_code=404, headers=headers)
    if outcome.status_code == 400:
        return JSONResponse({"detail": "bad_request"}, status_code=400, headers=headers)
    if outcome.status_code == 503 and outcome.body is not None:
        return JSONResponse(outcome.body, status_code=503, headers=headers)
    return Response(status_code=outcome.status_code, headers=headers)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else request.url.path


def _segments(resource: str, ev_date: str | None) -> tuple[str, ...]:
    base = ("aggregates", resource)
    if ev_date:
        return (*base, ev_date)
    return base


def _is_valid_request(source_election_id: str, resource: str, ev_date: str | None) -> bool:
    """False for an ev_date, election id, or path segment the API would never serve (400)."""
    if ev_date is not None and _EV_DATE_RE.fullmatch(ev_date) is None:
        return False
    try:
        http_path(source_election_id, *_segments(resource, ev_date))
    except ValueError:
        return False
    return True


def create_app(deps: ReadDeps | None = None) -> FastAPI:
    app = FastAPI(title="Early vote read API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled %s route=%s", type(exc).__name__, _route_template(request))
        return JSONResponse({"detail": "internal_error"}, status_code=500)

    app.state.read_deps = deps or ReadDeps(
        kv=_ClosedKv(),
        store=_ClosedStore(),
        access_client_id="",
        access_client_secret="",
    )

    async def _serve(
        source_election_id: str,
        resource: str,
        request: Request,
        ev_date: str | None = None,
    ) -> Response:
        active = _deps_for_request(request)
        if not await read_request_authorized(
            request.headers,
            access_client_id=active.access_client_id,
            access_client_secret=active.access_client_secret,
            kv=active.kv,
            token_db=active.token_db,
        ):
            return Response(status_code=401)
        if not _is_valid_request(source_election_id, resource, ev_date):
            return JSONResponse({"detail": "bad_request"}, status_code=400)
        segments = _segments(resource, ev_date)
        outcome = await read_resource(
            ResourceQuery(
                source_election_id=source_election_id,
                segments=segments,
                if_none_match=request.headers.get("if-none-match"),
            ),
            active.kv,
            active.store,
            ttl_seconds=active.ttl_seconds,
        )
        return outcome_response(outcome)

    @app.get("/health")
    async def health(request: Request) -> Response:
        active = _deps_for_request(request)
        if not await read_request_authorized(
            request.headers,
            access_client_id=active.access_client_id,
            access_client_secret=active.access_client_secret,
            kv=active.kv,
            token_db=active.token_db,
        ):
            return Response(status_code=401)
        return JSONResponse({"status": "ok"})

    @app.get("/elections/{source_election_id}/aggregates/totals")
    async def aggregates_totals(
        source_election_id: str,
        request: Request,
        ev_date: str | None = Query(default=None),
    ) -> Response:
        return await _serve(source_election_id, "totals", request, ev_date)

    @app.get("/elections/{source_election_id}/aggregates/county")
    async def aggregates_county(
        source_election_id: str,
        request: Request,
        ev_date: str | None = Query(default=None),
    ) -> Response:
        return await _serve(source_election_id, "county", request, ev_date)

    @app.get("/elections/{source_election_id}/aggregates/by-method")
    async def aggregates_by_method(
        source_election_id: str,
        request: Request,
        ev_date: str | None = Query(default=None),
    ) -> Response:
        return await _serve(source_election_id, "by-method", request, ev_date)

    @app.get("/elections/{source_election_id}/aggregates/by-day")
    async def aggregates_by_day(source_election_id: str, request: Request) -> Response:
        return await _serve(source_election_id, "by-day", request, None)

    @app.get("/elections/{source_election_id}/aggregates/county-by-day")
    async def aggregates_county_by_day(source_election_id: str, request: Request) -> Response:
        return await _serve(source_election_id, "county-by-day", request, None)

    return app


app = create_app()

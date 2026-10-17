"""FastAPI read API. The Worker mounts this with ``workers.asgi.entrypoint``.

https://developers.cloudflare.com/workers/languages/python/packages/fastapi/
https://fastapi.tiangolo.com/advanced/response-directly/

Bindings are not read here at import time. On the Worker, ``request.scope["env"]``
is set by workers-py ``request_to_scope``. Tests inject stores through
``create_app`` instead, because ``scope["env"]`` is absent under TestClient.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ev_api.auth import access_authorized
from ev_api.bindings import WorkersElectionStore, WorkersKvNamespace, binding_map_from_env
from ev_api.read import (
    CountyQuery,
    ElectionStore,
    KvNamespace,
    ReadOutcome,
    read_county,
    response_headers,
)


@dataclass(frozen=True)
class ReadDeps:
    kv: KvNamespace
    store: ElectionStore
    access_client_id: str
    access_client_secret: str
    ttl_seconds: int | None = None


class _ClosedKv:
    async def get(self, key: str) -> str | None:
        return None

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        return None


class _ClosedStore:
    async def fetch_body(self, source_election_id: str, resource: str) -> str | None:
        return None


def deps_from_env(env: object) -> ReadDeps:
    kv_namespace = getattr(env, "EV_KV", None)
    kv: KvNamespace = _ClosedKv() if kv_namespace is None else WorkersKvNamespace(kv_namespace)
    mapping = binding_map_from_env(getattr(env, "EV_D1_BINDINGS", ""))
    store: ElectionStore = WorkersElectionStore(env, mapping)
    client_id = getattr(env, "ACCESS_CLIENT_ID", "") or ""
    client_secret = getattr(env, "ACCESS_CLIENT_SECRET", "") or ""
    return ReadDeps(
        kv=kv,
        store=store,
        access_client_id=str(client_id),
        access_client_secret=str(client_secret),
        ttl_seconds=None,
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
    return Response(status_code=outcome.status_code, headers=headers)


def create_app(deps: ReadDeps | None = None) -> FastAPI:
    """Build the app. Docs routes stay off so preview has no public catalog."""
    app = FastAPI(title="Early vote read API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.read_deps = deps or ReadDeps(
        kv=_ClosedKv(),
        store=_ClosedStore(),
        access_client_id="",
        access_client_secret="",
        ttl_seconds=None,
    )

    @app.get("/elections/{source_election_id}/county/{county_id}")
    async def get_county(source_election_id: str, county_id: str, request: Request) -> Response:
        active = _deps_for_request(request)
        if not access_authorized(
            request.headers,
            client_id=active.access_client_id,
            client_secret=active.access_client_secret,
        ):
            return Response(status_code=401)
        try:
            outcome = await read_county(
                CountyQuery(
                    source_election_id=source_election_id,
                    county_id=county_id,
                    if_none_match=request.headers.get("if-none-match"),
                ),
                active.kv,
                active.store,
                ttl_seconds=active.ttl_seconds,
            )
        except ValueError:
            return Response(status_code=404)
        return outcome_response(outcome)

    return app


app = create_app()

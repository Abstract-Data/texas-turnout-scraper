#!/usr/bin/env bash
# Import + in-process HTTP smoke for the preview Worker Pyodide bundle.
# Mirrors deploy-time top-level imports and exercises FastAPI routes without Cloudflare.
# Run from repo root or infra/preview/worker after `uv run pywrangler sync --force`.
set -euo pipefail

worker_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${worker_dir}"

shopt -s nullglob
wasm_exts=(python_modules/**/*.cpython-*-wasm32-emscripten.so)
shopt -u nullglob

if ((${#wasm_exts[@]} == 0)); then
  echo "No PyEmscripten extension modules under python_modules/."
  echo "Run: cd infra/preview/worker && uv run pywrangler sync --force"
  echo "If sync succeeded but .so files are missing from git, check root .gitignore (*.so exception)."
  exit 1
fi

required="python_modules/pydantic_core/_pydantic_core.cpython-314-wasm32-emscripten.so"
if [[ ! -f "${required}" ]]; then
  echo "Missing required extension: ${required}"
  exit 1
fi

while IFS= read -r -d '' native_so; do
  case "${native_so}" in
    *wasm32-emscripten*) ;;
    *)
      echo "Non-PyEmscripten extension in bundle (host .so leak): ${native_so}"
      exit 1
      ;;
  esac
done < <(find "${worker_dir}/python_modules" -name '*.so' -print0)

pyodide_venv="${worker_dir}/.venv-workers/pyodide-venv"
if [[ ! -f "${pyodide_venv}/bin/activate" ]]; then
  echo "Pyodide venv not found at ${pyodide_venv}; run pywrangler sync first."
  exit 1
fi

# shellcheck source=/dev/null
source "${pyodide_venv}/bin/activate"
export PYTHONPATH="${worker_dir}/python_modules:${worker_dir}/src:${worker_dir}"
repo_root="$(cd "${worker_dir}/../../.." && pwd)"
export TX_TURNOUT_AGG_BINDING="$(
  PYTHONPATH="${repo_root}" python3 -c "from tests.verify.preview_tf_bindings import preview_election_d1_binding_name; print(preview_election_d1_binding_name())"
)"

# Isolate from host OS: no zoneinfo DB, no home/cwd config, UTC clock, no sockets.
smoke_root="$(mktemp -d)"
cleanup_smoke() {
  rm -rf "${smoke_root}"
}
trap cleanup_smoke EXIT

export PYTHONTZPATH="${smoke_root}/empty-tz"
export TZDIR="${PYTHONTZPATH}"
export HOME="${smoke_root}/empty-home"
export TZ=UTC
export LANG=C
export LC_ALL=C
mkdir -p "${PYTHONTZPATH}" "${HOME}"
smoke_cwd="${smoke_root}/empty-cwd"
mkdir -p "${smoke_cwd}"
cd "${smoke_cwd}"

if [[ ! -d "${worker_dir}/python_modules/tzdata" ]]; then
  echo "Missing bundled tzdata under python_modules/ (add tzdata to worker pyproject.toml and pywrangler sync)."
  exit 1
fi

# Prefer network namespace isolation when available (matches no egress at import).
py_run=(python)
if command -v unshare >/dev/null 2>&1 && unshare -r -n true 2>/dev/null; then
  py_run=(unshare -r -n python)
fi

"${py_run[@]}" - <<'PY'
from __future__ import annotations

import asyncio
import os
import socket
import sys
import time

# Block real sockets so import-time network fails here (Workers have no host egress).
def _deny_socket(*_args: object, **_kwargs: object) -> socket.socket:
    raise OSError("pyodide smoke: socket use blocked")


socket.socket = _deny_socket  # type: ignore[misc,assignment]

from httpx import ASGITransport, AsyncClient
from pydantic_core import ArgsKwargs

from zoneinfo import ZoneInfo

# Prove tzdata is bundled (not the host zoneinfo DB).
_ = ZoneInfo("America/Chicago")

# Mirror Worker main (entry.py) top-level import before ASGI mount.
import entry as _worker_entry  # noqa: F401

from ev_api.app import ReadDeps, _ClosedKv, _ClosedStore, create_app
from ev_api.bindings import WorkersAggregateStore
from texas_turnout_scraper.civix.read_client import http_path, version_key
from texas_turnout_scraper.consumer_tokens import hash_token, kv_key_for_hash
from texas_turnout_scraper.early_vote_sync import hash_key, sha256_decoded

assert create_app().title == "Early vote read API"
_ = ArgsKwargs((), {})
print("pyodide startup import smoke: OK")

class _SmokeKv:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self._values[key] = value


_consumer_plaintext = "pyodide-smoke-consumer-credential"
_consumer_digest = hash_token(_consumer_plaintext)
_smoke_kv = _SmokeKv({kv_key_for_hash(_consumer_digest): "1"})

# Stub D1/KV via in-process deps (no Cloudflare bindings).
app = create_app(
    ReadDeps(
        kv=_smoke_kv,
        store=_ClosedStore(),
        access_client_id="pyodide-smoke-client-id",
        access_client_secret="pyodide-smoke-client-secret",
    )
)


protected_path = http_path("49664", "aggregates", "totals")
health_path = "/health"


async def _http_smoke() -> tuple[int, int, int, int, int, float]:
    transport = ASGITransport(app=app)
    access_headers = {
        "CF-Access-Client-Id": app.state.read_deps.access_client_id,
        "CF-Access-Client-Secret": app.state.read_deps.access_client_secret,
    }
    bearer_unknown = {"Authorization": "Bearer pyodide-smoke-unknown-token"}
    bearer_valid = {"Authorization": f"Bearer {_consumer_plaintext}"}
    async with AsyncClient(transport=transport, base_url="http://pyodide-smoke") as client:
        health = await client.get(health_path)
        protected = await client.get(protected_path)
        health_authorized = await client.get(health_path, headers=access_headers)
        bearer_t0 = time.perf_counter()
        health_bearer_unknown = await client.get(health_path, headers=bearer_unknown)
        health_bearer_valid = await client.get(health_path, headers=bearer_valid)
        bearer_wall_ms = (time.perf_counter() - bearer_t0) * 1000.0
        return (
            health.status_code,
            protected.status_code,
            health_authorized.status_code,
            health_bearer_unknown.status_code,
            health_bearer_valid.status_code,
            bearer_wall_ms,
        )


(
    health_status,
    protected_status,
    health_authorized_status,
    health_bearer_unknown_status,
    health_bearer_valid_status,
    bearer_auth_wall_ms,
) = asyncio.run(_http_smoke())

print(f"pyodide http GET /health status={health_status}")
print(f"pyodide http GET {protected_path} unauthenticated status={protected_status}")
print(f"pyodide http GET /health authorized status={health_authorized_status}")
print(f"pyodide http GET /health bearer-unknown status={health_bearer_unknown_status}")
print(f"pyodide http GET /health bearer-valid status={health_bearer_valid_status}")
print(f"pyodide http bearer-auth wall_ms={bearer_auth_wall_ms:.1f}")

if health_status not in {401, 403}:
    print("expected unauthenticated GET /health status=401 or 403", file=sys.stderr)
    sys.exit(1)
if protected_status not in {401, 403}:
    print("expected unauthenticated protected route status=401 or 403", file=sys.stderr)
    sys.exit(1)
if health_authorized_status != 200:
    print("expected authorized GET /health status=200", file=sys.stderr)
    sys.exit(1)
if health_bearer_unknown_status != 401:
    print("expected bearer-unknown GET /health status=401", file=sys.stderr)
    sys.exit(1)
if health_bearer_valid_status != 200:
    print("expected bearer-valid GET /health status=200", file=sys.stderr)
    sys.exit(1)
if bearer_auth_wall_ms >= 2000.0:
    print("expected bearer-auth wall_ms < 2000", file=sys.stderr)
    sys.exit(1)


class _Prepared:
    def __init__(self, db: object, sql: str) -> None:
        self._db = db
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _Prepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        return self._db.first(self._sql, self._args)

    async def all(self) -> object:
        return self._db.run(self._sql, self._args)

    async def run(self) -> dict[str, object]:
        return self._db.run(self._sql, self._args)


class _PreviewD1:
    """D1 stub: ``run()`` envelopes + numeric fields as strings (preview shape)."""

    def __init__(
        self,
        *,
        pointer: str,
        file_id: str,
        sha256: str,
        decoded: bytes,
        days: list[dict[str, str]],
    ) -> None:
        import base64

        from texas_turnout_scraper.early_vote_sync import iter_chunk_b64

        self._pointer = pointer
        self._file_id = file_id
        self._sha256 = sha256
        self._chunks = dict(enumerate(iter_chunk_b64(decoded)))
        self._chunk_b64 = self._chunks.get(0, "")
        self._days = days
        self._byte_length = len(decoded)

    def prepare(self, sql: str) -> _Prepared:
        return _Prepared(self, sql)

    def _sqlite_all(self, sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            cur = conn.execute(sql, args)
            columns = [col[0] for col in cur.description or []]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        finally:
            conn.close()

    def first(self, sql: str, args: tuple[object, ...]) -> object | None:
        if "early_vote_pointer" in sql:
            return {"file_id": self._pointer}
        if "early_vote_day" in sql and "ev_date = ?" in sql:
            ev_date = str(args[1])
            for day in self._days:
                if day["ev_date"] == ev_date:
                    return day
            return None
        if "byte_length" in sql:
            return {"byte_length": self._byte_length}
        if "chunk_index = ?" in sql and "payload_b64" in sql:
            index = int(args[2])
            payload = self._chunks.get(index)
            return {"payload_b64": payload} if payload else None
        if "json_each(?1" in sql:
            rows = self._sqlite_all(sql, args)
            return rows[0] if rows else None
        return None

    def run(self, sql: str, args: tuple[object, ...]) -> dict[str, object]:
        if "FROM early_vote_file" in sql:
            return {
                "results": [
                    {
                        "file_id": self._file_id,
                        "sha256": self._sha256,
                        "chunk_count": "1",
                        "chunks_present": "1",
                    }
                ],
                "success": True,
            }
        if "chunk_index FROM early_vote_chunk" in sql.replace("\n", " "):
            return {
                "results": [{"chunk_index": str(index)} for index in sorted(self._chunks)],
                "success": True,
            }
        if "FROM early_vote_chunk" in sql and "chunk_index = ?" not in sql:
            return {
                "results": [{"chunk_index": "0", "payload_b64": self._chunk_b64}],
                "success": True,
            }
        if "FROM early_vote_day" in sql and "ORDER BY" in sql:
            return {"results": list(self._days), "success": True}
        if "json_each(?1" in sql:
            return {"results": self._sqlite_all(sql, args), "success": True}
        return {"results": [], "success": True}


_AGG_ELECTION = "53815"
_AGG_BINDING = os.environ.get("TX_TURNOUT_AGG_BINDING", "")
if not _AGG_BINDING:
    print("TX_TURNOUT_AGG_BINDING is empty", file=sys.stderr)
    sys.exit(1)
_AGG_VERSION = 7
_turnout_payload = (
    b'{"turnout_by_county":[{"name":"TRAVIS",'
    b'"total_in_person_votes_for_election":10,"total_mail_votes_for_election":2}]}'
)
_AGG_SHA = sha256_decoded(_turnout_payload)
_day_row = {
    "ev_date": "2026-10-20",
    "file_id": "f53815",
    "sha256": _AGG_SHA,
}
_preview_db = _PreviewD1(
    pointer="f53815",
    file_id="f53815",
    sha256=_AGG_SHA,
    decoded=_turnout_payload,
    days=[_day_row],
)


class _Env:
    pass


setattr(_Env, _AGG_BINDING, _preview_db)


_agg_routes = (
    "totals",
    "county",
    "by-method",
    "by-day",
    "county-by-day",
)


def _agg_app(kv: _SmokeKv) -> object:
    return create_app(
        ReadDeps(
            kv=kv,
            store=WorkersAggregateStore(_Env(), {_AGG_ELECTION: _AGG_BINDING}),
            access_client_id=app.state.read_deps.access_client_id,
            access_client_secret=app.state.read_deps.access_client_secret,
        )
    )


agg_kv = _SmokeKv(
    {
        version_key(_AGG_ELECTION): str(_AGG_VERSION),
        hash_key(_AGG_ELECTION): _AGG_SHA,
    }
)
agg_app = _agg_app(agg_kv)


class _EnvMissingBinding:
    pass


setattr(_EnvMissingBinding, "DB", _preview_db)

miswired_kv = _SmokeKv(
    {
        version_key(_AGG_ELECTION): str(_AGG_VERSION),
        hash_key(_AGG_ELECTION): _AGG_SHA,
    }
)
miswired_app = create_app(
    ReadDeps(
        kv=miswired_kv,
        store=WorkersAggregateStore(_EnvMissingBinding(), {_AGG_ELECTION: _AGG_BINDING}),
        access_client_id=app.state.read_deps.access_client_id,
        access_client_secret=app.state.read_deps.access_client_secret,
    )
)


async def _miswired_binding_smoke() -> int:
    transport = ASGITransport(app=miswired_app)
    access_headers = {
        "CF-Access-Client-Id": miswired_app.state.read_deps.access_client_id,
        "CF-Access-Client-Secret": miswired_app.state.read_deps.access_client_secret,
    }
    path = http_path(_AGG_ELECTION, "aggregates", "totals")
    async with AsyncClient(transport=transport, base_url="http://pyodide-smoke") as client:
        response = await client.get(path, headers=access_headers)
    return response.status_code


miswired_status = asyncio.run(_miswired_binding_smoke())
print(f"aggregate miswired-binding status={miswired_status}")
if miswired_status != 404:
    print("expected miswired-binding aggregate status=404", file=sys.stderr)
    sys.exit(1)

_no_days_db = _PreviewD1(
    pointer="f53815",
    file_id="f53815",
    sha256=_AGG_SHA,
    decoded=_turnout_payload,
    days=[],
)


class _EnvNoDays:
    pass


setattr(_EnvNoDays, _AGG_BINDING, _no_days_db)


empty_days_kv = _SmokeKv(
    {
        version_key(_AGG_ELECTION): str(_AGG_VERSION),
        hash_key(_AGG_ELECTION): _AGG_SHA,
    }
)
county_by_day_empty_app = create_app(
    ReadDeps(
        kv=empty_days_kv,
        store=WorkersAggregateStore(_EnvNoDays(), {_AGG_ELECTION: _AGG_BINDING}),
        access_client_id=app.state.read_deps.access_client_id,
        access_client_secret=app.state.read_deps.access_client_secret,
    )
)


async def _route_roundtrip(
    client: AsyncClient,
    path: str,
    access_headers: dict[str, str],
) -> tuple[int, bool, int]:
    first = await client.get(path, headers=access_headers)
    etag = first.headers.get("etag")
    second = await client.get(
        path,
        headers={**access_headers, "If-None-Match": etag or ""},
    )
    return first.status_code, bool(etag), second.status_code


async def _aggregate_smoke() -> list[tuple[str, int, bool, int]]:
    transport = ASGITransport(app=agg_app)
    access_headers = {
        "CF-Access-Client-Id": agg_app.state.read_deps.access_client_id,
        "CF-Access-Client-Secret": agg_app.state.read_deps.access_client_secret,
    }
    outcomes: list[tuple[str, int, bool, int]] = []
    async with AsyncClient(transport=transport, base_url="http://pyodide-smoke") as client:
        for resource in _agg_routes:
            path = http_path(_AGG_ELECTION, "aggregates", resource)
            status, etag_ok, inm = await _route_roundtrip(client, path, access_headers)
            outcomes.append((resource, status, etag_ok, inm))
        missing = await client.get(
            http_path("99999", "aggregates", "totals"),
            headers=access_headers,
        )
        outcomes.append(("unknown-election", missing.status_code, False, 0))
    empty_transport = ASGITransport(app=county_by_day_empty_app)
    async with AsyncClient(transport=empty_transport, base_url="http://pyodide-smoke") as client:
        path = http_path(_AGG_ELECTION, "aggregates", "county-by-day")
        status, etag_ok, inm = await _route_roundtrip(client, path, access_headers)
        outcomes.append(("county-by-day-empty", status, etag_ok, inm))
    return outcomes


agg_outcomes = asyncio.run(_aggregate_smoke())

for label, status, etag_ok, inm in agg_outcomes:
    if label == "unknown-election":
        print(f"empty-election status={status}")
        continue
    if label == "county-by-day-empty":
        print(f"aggregate county-by-day-empty authorized status={status}")
        print(f"aggregate county-by-day-empty etag present={'yes' if etag_ok else 'no'}")
        print(f"aggregate county-by-day-empty if-none-match status={inm}")
        continue
    print(f"aggregate {label} authorized-200 status={status}")
    print(f"aggregate {label} etag present={'yes' if etag_ok else 'no'}")
    print(f"aggregate {label} if-none-match status={inm}")

for label, status, etag_ok, inm in agg_outcomes:
    if label == "unknown-election":
        if status != 404:
            print("expected empty-election status=404", file=sys.stderr)
            sys.exit(1)
        continue
    if label == "county-by-day-empty":
        if status != 404:
            print("expected county-by-day-empty status=404", file=sys.stderr)
            sys.exit(1)
        if not etag_ok:
            print("expected etag on county-by-day-empty", file=sys.stderr)
            sys.exit(1)
        if inm != 304:
            print("expected county-by-day-empty if-none-match status=304", file=sys.stderr)
            sys.exit(1)
        continue
    if status != 200:
        print(f"expected aggregate {label} status=200", file=sys.stderr)
        sys.exit(1)
    if not etag_ok:
        print(f"expected etag on aggregate {label}", file=sys.stderr)
        sys.exit(1)
    if inm != 304:
        print(f"expected aggregate {label} if-none-match status=304", file=sys.stderr)
        sys.exit(1)

print(f"aggregate binding name from terraform={_AGG_BINDING}")
print("pyodide http startup smoke: OK")
PY

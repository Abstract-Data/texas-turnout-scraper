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
from texas_turnout_scraper.civix.read_client import http_path
from texas_turnout_scraper.consumer_tokens import hash_token, kv_key_for_hash

assert create_app().title == "Early vote read API"
_ = ArgsKwargs((), {})
print("pyodide startup import smoke: OK")

class _SmokeKv:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get(self, key: str) -> str | None:
        return self._values.get(key)


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

print("pyodide http startup smoke: OK")
PY

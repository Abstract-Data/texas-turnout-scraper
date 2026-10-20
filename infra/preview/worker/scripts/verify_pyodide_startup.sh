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

pyodide_venv="${worker_dir}/.venv-workers/pyodide-venv"
if [[ ! -f "${pyodide_venv}/bin/activate" ]]; then
  echo "Pyodide venv not found at ${pyodide_venv}; run pywrangler sync first."
  exit 1
fi

# shellcheck source=/dev/null
source "${pyodide_venv}/bin/activate"
export PYTHONPATH="${worker_dir}/python_modules:${worker_dir}/src"

python - <<'PY'
from __future__ import annotations

import asyncio
import sys

from httpx import ASGITransport, AsyncClient
from pydantic_core import ArgsKwargs

from ev_api.app import ReadDeps, _ClosedKv, _ClosedStore, create_app
from texas_turnout_scraper.civix.read_client import http_path

assert create_app().title == "Early vote read API"
_ = ArgsKwargs((), {})
print("pyodide startup import smoke: OK")

# Stub D1/KV via closed in-process deps (no Cloudflare bindings).
app = create_app(
    ReadDeps(
        kv=_ClosedKv(),
        store=_ClosedStore(),
        access_client_id="pyodide-smoke-client-id",
        access_client_secret="pyodide-smoke-client-secret",
    )
)


@app.get("/health")
async def _health() -> dict[str, str]:
    return {"status": "ok"}


protected_path = http_path("49664", "aggregates", "totals")


async def _http_smoke() -> tuple[int, int]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://pyodide-smoke") as client:
        health = await client.get("/health")
        protected = await client.get(protected_path)
        return health.status_code, protected.status_code


health_status, protected_status = asyncio.run(_http_smoke())

print(f"pyodide http GET /health status={health_status}")
print(f"pyodide http GET {protected_path} unauthenticated status={protected_status}")

if health_status != 200:
    print("expected GET /health status=200", file=sys.stderr)
    sys.exit(1)
if protected_status not in {401, 403}:
    print("expected unauthenticated protected route status=401 or 403", file=sys.stderr)
    sys.exit(1)

print("pyodide http startup smoke: OK")
PY

# Preview early-vote read Worker

Single deployable Worker for preview Terraform (`tx-turnout-ev-api-preview`).

| Path | Role |
|------|------|
| `entry.py` | `workers.asgi.entrypoint` mounting the FastAPI app |
| `src/ev_api/` | Aggregate read API (KV cache, D1 chunk reads, Access gate) |
| `python_modules/` | Pyodide bundle from `uv run pywrangler sync` (runtime SDK, FastAPI, `texas_turnout_scraper`) |

The former `workers/ev-api/` tree is merged here so Terraform (`worker_bundle.tf`) ships one module list: `entry.py`, `ev_api/**`, and vendored `python_modules/**`.

**~60s KV propagation is not fit for election-night results.** Responses use versioned keys `ev:<election>:v<N>:<path>`, ETag `v<N>`, optional `If-None-Match` → 304, and `expirationTtl` on cache writes (default 24h). D1 is queried only on a cache miss.

## Regenerate the Pyodide bundle

```bash
cd infra/preview/worker
uv sync
uv run pywrangler sync --force
```

Commit `python_modules/`, `pylock.toml`, and `uv.lock` when dependencies change.

PyEmscripten native modules (`*.cpython-*-wasm32-emscripten.so` under `python_modules/`) must be committed. Root `.gitignore` ignores `*.so` globally but un-ignores that path; without the `.so` files, Workers deploy fails at script validation (`ImportError` for `pydantic_core.ArgsKwargs`).

CI runs `scripts/verify_pyodide_startup.sh` after `pywrangler sync` to catch missing extensions and import failures before Terraform apply.

## HTTP surface (Access service token required)

- `GET /elections/{source_election_id}/aggregates/totals?ev_date=`
- `GET /elections/{source_election_id}/aggregates/county?ev_date=`
- `GET /elections/{source_election_id}/aggregates/by-method?ev_date=`
- `GET /elections/{source_election_id}/aggregates/by-day`
- `GET /elections/{source_election_id}/aggregates/county-by-day`

Row-level roster routes are intentionally omitted; aggregates only.

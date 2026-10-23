# Preview early-vote read Worker

Single deployable Worker for preview Terraform (`tx-turnout-ev-api-preview`).

| Path | Role |
|------|------|
| `entry.py` | `workers.asgi.entrypoint` mounting the FastAPI app |
| `src/ev_api/` | Aggregate read API (KV cache, D1 chunk reads, Access gate) |
| `python_modules/` | Pyodide bundle from `uv run pywrangler sync` (runtime SDK, FastAPI, `tzdata`, `texas_turnout_scraper`) |

The former `workers/ev-api/` tree is merged here so Terraform (`worker_bundle.tf`) ships one module list: `entry.py`, `ev_api/**`, and vendored `python_modules/**`.

**~60s KV propagation is not fit for election-night results.** Responses use versioned keys `ev:<election>:v<N>:<path>`, ETag `v<N>`, optional `If-None-Match` → 304, and `expirationTtl` on cache writes (default 24h). D1 is queried only on a cache miss.

**Cold-miss stampede guard (best-effort):** concurrent misses on the same election, version, and aggregate endpoint coordinate with a KV lock key `ev:<election>:v<N>:lock:<endpoint>` (60s TTL, Cloudflare minimum). A request that sees the lock rechecks the versioned cache once; if still empty it returns **503** with `Retry-After` and `{"detail":"aggregate_in_progress"}` instead of starting another full D1 chunk walk. KV `get`/`put` are **not atomic**, so the lock narrows overlap but does not eliminate it.

## Regenerate the Pyodide bundle

```bash
cd infra/preview/worker
uv sync
uv run pywrangler sync --force
```

Commit `python_modules/`, `pylock.toml`, and `uv.lock` when dependencies change.

PyEmscripten native modules (`*.cpython-*-wasm32-emscripten.so` under `python_modules/`) must be committed. Root `.gitignore` ignores `*.so` globally but un-ignores that path; without the `.so` files, Workers deploy fails at script validation (`ImportError` for `pydantic_core.ArgsKwargs`).

CI runs `scripts/verify_pyodide_startup.sh` after `pywrangler sync` to catch missing extensions and import failures before Terraform apply.

## Keep vendored tzdata current

Pyodide has no host time zone database, so the Worker ships the pure-Python `tzdata` wheel in `python_modules/`. The copy is static, so DST and zone-rule changes only reach the Worker when it is bumped.

`.github/workflows/tzdata-update.yml` runs weekly (and on demand). `scripts/check_tzdata_update.py` compares the vendored version with the latest stable PyPI release. When PyPI is newer, the job runs `uv lock --upgrade-package tzdata` and `pywrangler sync --force`, then `scripts/verify_pyodide_startup.sh` as a required gate, and only then opens a PR containing `uv.lock`, `pylock.toml`, and the vendored `tzdata` files. It never merges and never deploys. A PR opened with `GITHUB_TOKEN` does not start other workflows, so close and reopen it to run the Terraform preview checks.

Dependabot is deliberately not enabled for this directory. It can bump `uv.lock` but cannot refresh `pylock.toml` or `python_modules/`, so its PRs would fail the pywrangler drift check in `terraform-preview.yml`.

Manual bump: `cd infra/preview/worker && uv lock --upgrade-package tzdata && uv sync --frozen && uv run pywrangler sync --force && bash scripts/verify_pyodide_startup.sh`.

## HTTP surface (Access service token or consumer bearer/API key)

- `GET /elections/{source_election_id}/aggregates/totals?ev_date=`
- `GET /elections/{source_election_id}/aggregates/county?ev_date=`
- `GET /elections/{source_election_id}/aggregates/by-method?ev_date=`
- `GET /elections/{source_election_id}/aggregates/by-day`
- `GET /elections/{source_election_id}/aggregates/county-by-day`

Row-level roster routes are intentionally omitted; aggregates only.

# Early-vote ingest Container (ADR-017)

Worker `tx-turnout-ev-ingest-preview` with one Durable Object, `EvIngest`, that starts a Container
from `Dockerfile.ev-ingest` (repo root, the package with the `waf` and `loader` extras).

Step 1 is a portal egress spike. `POST /spike` starts the Container, runs
`early_vote_sync --dry-run` for one day, destroys the Container and returns the exit code, the
seconds taken and the report's `status`, `decoded_bytes`, `sha256` and `within_cap` lines. A dry
run writes nothing. There is no cron yet.

## Deploy (owner)

Docker must be running; `wrangler deploy` builds the image and pushes it to Cloudflare's registry.

```bash
cd infra/ev_ingest
npm ci
openssl rand -hex 32 | tee /dev/stderr | npx wrangler secret put RUN_TOKEN   # keep the printed token
npx wrangler deploy
```

The first deploy can take several minutes before the Container starts.

## Run the spike

```bash
curl -sS -X POST "https://tx-turnout-ev-ingest-preview.<subdomain>.workers.dev/spike?date=10/24/2026" \
  -H "Authorization: Bearer $RUN_TOKEN"
```

Without `date` it fetches the latest published day. Success is `"exit_code": 0` and a `decoded_bytes`
near the day's size; run locally in the same image on 2026-10-28, 10/24 decoded 29,648,664 bytes
in 10 s with a 304 MiB peak. A non-zero exit with no `decoded_bytes` means the portal refused the
Container; the Worker logs carry the error class.

## Checks

```bash
npx tsc --noEmit
npx wrangler deploy --dry-run --outdir /tmp/ev_ingest_dry
```

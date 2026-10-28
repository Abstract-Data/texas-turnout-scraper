# Early-vote ingest Container (ADR-017)

Worker `tx-turnout-ev-ingest-preview` with one Durable Object class, `EvIngest`, that starts a
Container from `Dockerfile.ev-ingest` (repo root: the package with the `waf` and `loader` extras and
`scripts/ev_ingest_steps.sh`).

- **Cron** `0 15,16,22,23,3,4 * * *` (UTC). At 10:00, 17:00 and 22:00 Central through 11:59 PM on
  2026-11-03 it starts a run (`src/schedule.ts`). The other three firings a day do nothing.
- **A run** is `scripts/ev_ingest_steps.sh <mode>` in a `standard-2` Container: the backfill sync,
  the aggregate precompute, the roster rows, `first_seen_build` and `geo_load`, the same steps as
  `scripts/local_ev_backfill.sh`. The Durable Object starts the Container and returns; an alarm
  every 60 s reads the exit code, logs the steps' `key=value` lines and destroys the Container.
  A run that passes 3 hours is destroyed. One run at a time.
- **`EV_SYNC_MODE`** (`wrangler.jsonc` vars): `off` (default, nothing runs), `dry_run` (hash every
  day, write nothing) or `apply`.

## Deploy (owner)

Docker must be running; `wrangler deploy` builds the image and pushes it to Cloudflare's registry.
The secrets are copies of the 1Password Environment `s6tljdcafkgqxfuxhcnj3m5x5a` values.

```bash
cd infra/ev_ingest
npm ci
export RUN_TOKEN="$(openssl rand -hex 32)"   # keep it for the requests below
printf '%s' "$RUN_TOKEN" | npx wrangler secret put RUN_TOKEN
op run --environment s6tljdcafkgqxfuxhcnj3m5x5a -- bash -c '
  for name in CLOUDFLARE_API_TOKEN R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
    printf "%s" "${!name}" | npx wrangler secret put "$name"
  done'
npx wrangler deploy
```

The first deploy can take several minutes before the Container starts.

## Check portal egress

```bash
URL="https://tx-turnout-ev-ingest-preview.<subdomain>.workers.dev"
curl -sS -X POST "$URL/spike?date=10/24/2026" -H "Authorization: Bearer $RUN_TOKEN"
```

`/spike` runs a dry-run sync for one day and destroys the Container. Without `date` it fetches the
latest published day. Success is `"exit_code": 0` and a `decoded_bytes` near the day's size (10/24
decoded 29,648,664 bytes locally on 2026-10-28). A non-zero exit with no `decoded_bytes` means the
portal refused the Container; the Worker logs carry the error class.

## Turn the schedule on

After the spike succeeds, set `"EV_SYNC_MODE": "apply"` in `wrangler.jsonc`, commit it and deploy.
A deploy resets the variable to the file's value, so a dashboard edit does not last. To try a full
run first, deploy with `dry_run` and start one by hand:

```bash
curl -sS -X POST "$URL/run" -H "Authorization: Bearer $RUN_TOKEN"     # 202, or 409 if running or off
curl -sS "$URL/status" -H "Authorization: Bearer $RUN_TOKEN"          # mode and the run in progress
```

Results are in Workers Logs: `ev-ingest started`, then `ev-ingest finished` with `exit_code`,
`seconds` and the report lines. To stop the schedule, set the mode back to `off` and deploy.

## Checks

```bash
npx tsc --noEmit
npm test
npx wrangler deploy --dry-run --outdir /tmp/ev_ingest_dry
```

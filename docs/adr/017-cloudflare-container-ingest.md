# ADR-017: Early-vote ingest in a Cloudflare Container

**Date:** 2026-10-28
**Status:** Proposed. Supersedes ADR-014 once accepted. Step 1, the portal egress spike (`POST /spike`), and step 2, the scheduled ingest, are implemented in `infra/ev_ingest`. The cron ships with `EV_SYNC_MODE=off` until the egress check passes.

## Context

The roster in `ev-53815` stops at 2026-10-24. The GitHub sync workflow is disabled, and ADR-014's replacement is not built: the sync Workflow's `fetch_day_bytes` raises `NotImplementedError`, and `EV_SYNC_MODE` is `off`.

ADR-014 rewrites the loader to fit a Worker's 128 MB isolate: each day split into 16 hash ranges, one Workflow step per range. Its own table lists six loader functions that hold a whole 48 MB day or more; `repeat_build` peaked at 802 MB. That rewrite is the whole cost of ADR-014, and the election is 2026-11-03.

The loader already runs unchanged outside GitHub. `scripts/local_ev_backfill.sh` runs the apply job's steps on a laptop with `op run`: the sync, the aggregate precompute, the roster rows, `first_seen_build` and `geo_load`.

## Decision

Run the existing Python loader, unchanged, in a Cloudflare Container started by a Worker cron.

- **Image.** Python 3.12, the package installed with `uv sync --locked --extra waf --extra loader`. The five steps live in one script, `scripts/ev_ingest_steps.sh`, which both the Container and `scripts/local_ev_backfill.sh` run. `repeat_build` stays paused (ADR-014).
- **Size.** `standard-2` (1 vCPU, 6 GiB) to start. Cloudflare's limits page lists up to 4 vCPU and 12 GiB per instance, so `repeat_build` fits later.
- **Trigger.** The cron moves from the Python sync Worker to a small TypeScript Worker (`infra/ev_ingest`). Its `scheduled` handler calls a Durable Object, which starts the Container. Every Containers example and the `ctx.container` API reference are JavaScript or TypeScript; nothing shows a Python Worker starting a Container. The schedule (`0 15,16,22,23,3,4 * * *` UTC, cut off at 11:59 PM Central on 2026-11-03) and the `EV_SYNC_MODE` gate carry over. The Terraform-managed `cloudflare_workers_cron_trigger.ev_sync` on the Python sync Worker is removed in the same change that gives the new Worker its cron, so the schedule never fires twice.
- **Run length.** The Container has no fixed maximum runtime; the Worker invocation that starts it does. Cloudflare's cron example awaits `monitor()`, but its own page says to keep the task within Cron Trigger limits, and a pending `monitor()` holds the Durable Object for at most 15 minutes. So the Durable Object starts the Container, sets `setInactivityTimeout` and an alarm, and returns. Code inside the Container does not count as activity, and `exec()` does not inherit the variables `start()` sets, so the steps run as the Container's entrypoint: they record their exit code in a file and the entrypoint then idles. The alarm runs every 60 s, which keeps the Durable Object active; it reads the exit code, logs the steps' `key=value` lines, and destroys the Container. A run past 3 hours is destroyed. One run at a time: the Durable Object refuses a start while one is running. This shape is for the cron ingest; the step 1 `/spike` route awaits its 10-second dry run inside the request and does not guard against a second concurrent request.
- **Secrets.** `CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` are Worker secrets, passed to the Container through `start({ env })`, replacing `op run`. `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_NAMESPACE_ID` and `EV_SNAPSHOT_BUCKET` are plain variables. 1Password Environment `s6tljdcafkgqxfuxhcnj3m5x5a` stays the source of truth; the Worker secrets are copies, set with `wrangler secret put` and re-set when a value rotates. The Container needs internet access for the portal and the Cloudflare REST API.
- **Writes.** Unchanged. The loader writes D1 and KV through the REST API, so a full rewrite of changed days still takes tens of minutes. Nothing in a Container caps that.
- **Interruption.** A host restart sends SIGTERM and waits up to 15 minutes. Every step is already idempotent (a day whose hash matches is skipped; the counts and geo builds compare signatures), so the next cron run finishes the work.

## Deploys

Cloudflare Terraform provider 5.28.0, pinned here, has no Container resource. Its binary carries the `containers/applications` API path but defines no resource for it. Containers deploy with `wrangler deploy`, which builds the image with Docker and pushes it to Cloudflare's registry. ADR-014 R5 says a wrangler-only resource breaks the Terraform-managed rule and goes to the owner.

The owner accepted (2026-10-28) an exception for this one Worker:

- **Normal deploys: Workers Builds** on push to `main`, running `npx wrangler deploy`. Cloudflare's Containers deploy guide says Dockerfile builds can run in the Workers Builds environment on the production branch; non-production branches run `wrangler versions upload`, which does not publish an image.
- **First deploy and fallback: the owner runs `wrangler deploy`** with Docker running (`infra/ev_ingest/README.md`).
- Terraform keeps every other resource, including the D1 database, KV namespace and R2 buckets the loader writes to.

## Evidence (2026-10-28)

Built from `Dockerfile.ev-ingest` for `linux/amd64` and run locally with Docker, with a 6 GiB memory cap:

- `early_vote_sync --dry-run --sync-mode single --date 10/24/2026` fetched the day through cloudscraper, decoded 29,648,664 bytes, exited 0 in 10 s, peak memory 304 MiB.
- `--sync-mode latest` picked 2026-10-28, which held 4 rows; 10/27 decoded 1,108,969 bytes. The portal was still publishing those days.
- The image carries the `waf` and `loader` extras and `data/geo/tx_geo.json`.
- `wrangler deploy --dry-run` built the same image and listed the `EvIngest` Container.

## Unverified

- Whether the Civix portal accepts requests from a Container's egress. The local run proves the image, not Cloudflare's network path. `POST /spike` on the deployed Worker settles it.
- Container start time and the cost of a 6 GiB instance for about 45 minutes, six times a day.
- Peak memory of an apply run. The dry run peaked at 304 MiB; the apply path also holds the stored version, so `standard-2` stays the starting size until a measured apply.

## Consequences

- ADR-014's per-range rewrite, its R2 staging bucket and the Python sync Worker are no longer needed for ingest. Removing them is a later change.
- The loader keeps one implementation, tested by the existing unit tests; the Container adds an image and a small Worker, not a second loader.
- `first_seen_build` runs in the Container, so the GitHub workflow step can be removed once the Container runs.

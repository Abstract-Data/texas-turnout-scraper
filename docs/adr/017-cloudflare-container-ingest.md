# ADR-017: Early-vote ingest in a Cloudflare Container

**Date:** 2026-10-28
**Status:** Proposed. Supersedes ADR-014 once accepted. Nothing is implemented.

## Context

The roster in `ev-53815` stops at 2026-10-24. The GitHub sync workflow is disabled, and ADR-014's replacement is not built: the sync Workflow's `fetch_day_bytes` raises `NotImplementedError`, and `EV_SYNC_MODE` is `off`.

ADR-014 rewrites the loader to fit a Worker's 128 MB isolate: each day split into 16 hash ranges, one Workflow step per range. Its own table lists six loader functions that hold a whole 48 MB day or more; `repeat_build` peaked at 802 MB. That rewrite is the whole cost of ADR-014, and the election is 2026-11-03.

The loader already runs unchanged outside GitHub. `scripts/local_ev_backfill.sh` runs the apply job's steps on a laptop with `op run`: the sync, the aggregate precompute, the roster rows, `first_seen_build` and `geo_load`.

## Decision

Run the existing Python loader, unchanged, in a Cloudflare Container started by a Worker cron.

- **Image.** Python 3.12, the package installed with `uv sync --locked --extra waf --extra loader`, entrypoint the same five steps as `scripts/local_ev_backfill.sh`. `repeat_build` stays paused (ADR-014).
- **Size.** `standard-2` (1 vCPU, 6 GiB) to start. Cloudflare's limits page lists up to 4 vCPU and 12 GiB per instance, so `repeat_build` fits later.
- **Trigger.** The cron moves from the Python sync Worker to a small TypeScript Worker. Its `scheduled` handler calls a Durable Object, which starts the Container. Every Containers example and the `ctx.container` API reference are JavaScript or TypeScript; nothing shows a Python Worker starting a Container. The schedule (`0 15,16,22,23,3,4 * * *` UTC, cut off at 11:59 PM Central on 2026-11-03) and the `EV_SYNC_MODE` gate carry over.
- **Run length.** Containers have no fixed maximum runtime. Code inside the Container does not count as activity, so the Durable Object sets an alarm every minute until the process exits, and `setInactivityTimeout` (at most 6 hours) covers gaps. One run at a time: the Durable Object refuses a start while one is running.
- **Secrets.** Worker secrets passed to the Container as environment variables, replacing `op run`. The Container needs internet access for the portal and the Cloudflare REST API.
- **Writes.** Unchanged. The loader writes D1 and KV through the REST API, so a full rewrite of changed days still takes tens of minutes. Nothing in a Container caps that.
- **Interruption.** A host restart sends SIGTERM and waits up to 15 minutes. Every step is already idempotent (a day whose hash matches is skipped; the counts and geo builds compare signatures), so the next cron run finishes the work.

## Open decision: deploys

Cloudflare Terraform provider 5.28.0, pinned here, has no Container resource. Its binary carries the `containers/applications` API path but defines no resource for it. Containers deploy with `wrangler deploy`, which builds the image with Docker and pushes it to Cloudflare's registry. ADR-014 R5 says a wrangler-only resource breaks the Terraform-managed rule and goes to the owner. Options:

1. Terraform keeps everything else; the Container Worker deploys with `wrangler deploy` run by the owner.
2. The same, deployed by Workers Builds on push to `main`, so no laptop deploy and no GitHub workflow.
3. Wait for a provider release that adds a Container resource. Not before 2026-11-03.

## Unverified

- Whether the Civix portal accepts requests from a Container's egress. The ADR-014 spike fetched a day from a Worker with HTTP 200; a Container leaves from a different network path. The first spike settles it.
- Container start time and the cost of a 6 GiB instance for about 45 minutes, six times a day.

## Consequences

- ADR-014's per-range rewrite, its R2 staging bucket and the Python sync Worker are no longer needed for ingest. Removing them is a later change.
- The loader keeps one implementation, tested by the existing unit tests; the Container adds an image and a small Worker, not a second loader.
- `first_seen_build` runs in the Container, so the GitHub workflow step can be removed once the Container runs.

# ADR-013: The preview Worker's cron trigger dispatches the early-vote sync

**Date:** 2026-10-24
**Status:** Accepted

## Context

The early-vote sync must run at 10:00, 17:00 and 22:00 America/Chicago until 11:59 PM on
2026-11-03, without anyone approving each run. PR #84 put a GitHub `schedule` on
`early-vote-sync.yml` with a `window` job that dropped the wrong hours. That had two problems.
GitHub delays and sometimes drops scheduled runs under load, so the times were not reliable.
And every apply ran in the `preview` environment, which needs a reviewer, so a scheduled run
waited for approval and was not unattended.

## Decision

1. **The Worker times the run, Actions does the work.** `cloudflare_workers_cron_trigger.ev_sync`
   fires the preview Worker `tx-turnout-ev-api-preview` on `0 15,16,22,23,3,4 * * *` (UTC). Its
   `scheduled` handler (`infra/preview/worker/entry.py`, logic in `ev_api/cron.py`) sends one
   `POST /repos/Abstract-Data/texas-turnout-scraper/actions/workflows/early-vote-sync.yml/dispatches`
   on `main` with `sync_mode=backfill`, `election_id=53815`, `payload_kind=statewide_roster`,
   `dry_run=false`, `election_date=""` and `trigger=cron`. The Worker does no scraping and writes
   nothing to D1 or KV. Scraping and ingest stay in the workflow, unchanged.
2. **No GitHub schedule.** The `schedule:` trigger and the `window` job are gone, so nothing runs
   twice. The workflow has only `workflow_dispatch`.
3. **Unattended runs use `preview-scheduled`.** The apply job picks
   `${{ inputs.trigger == 'cron' && 'preview-scheduled' || 'preview' }}`. `preview-scheduled` has
   no required reviewer, allows the `main` branch only, and holds the same secrets as `preview`.
   Manual applies and the Terraform workflow stay on `preview` with its reviewer.
4. **A cron run can only be the scheduled backfill.** The apply job's first step, before any secret
   is in a step's environment, fails unless a `trigger=cron` run has `sync_mode=backfill`,
   `election_id=53815`, `payload_kind=statewide_roster`, `dry_run=false` and
   `GITHUB_REF=refs/heads/main`.
5. **DST without tzdata.** Cron is UTC only. The trigger lists the CDT hours (15, 22, 03) and the
   CST hours (16, 23, 04). `should_dispatch` takes Central as UTC-5 before 2026-11-01 07:00 UTC and
   UTC-6 from then, and proceeds only when that gives hour 10, 17 or 22 and the time is at or before
   2026-11-04 05:59:59 UTC. The window holds one DST change, so two integer offsets cover it. This
   does not read the zone database and does not depend on the vendored `tzdata` being current.
6. **Token scope.** The Worker reads `GITHUB_DISPATCH_TOKEN`, a `secret_text` binding from the
   sensitive Terraform variable `github_dispatch_token`. CI fills it from the `preview` environment
   secret `EV_DISPATCH_GITHUB_TOKEN` in the plan step's `env:`. GitHub secret names cannot start
   with `GITHUB_`. The token is fine-grained, limited to this repository, with only
   **Actions: write**. That permission is what the dispatch endpoint needs. It cannot read secrets,
   push code or approve deployments.

## Consequences

- **Anyone who can dispatch the workflow can start an unattended apply** by choosing
  `trigger=cron`. Step 4 limits that run to the exact scheduled backfill on `main`, which is
  idempotent: unchanged days write nothing. Any other apply still needs a reviewer. The same is true
  of a leaked dispatch token: it can start the scheduled backfill early or more often, and nothing
  else.
- **A missing token skips.** Until the owner sets `EV_DISPATCH_GITHUB_TOKEN` and applies Terraform,
  the binding is absent and the handler logs `early-vote dispatch skipped: GITHUB_DISPATCH_TOKEN is
  not set` and returns.
- **A revoked or expired token fails loudly.** GitHub answers 401 or 403. The handler treats only 204
  as success and raises `DispatchError` naming the status code, which shows as a failed cron
  invocation in Workers Logs. The token, headers and response body are never logged. The request
  pins `X-GitHub-Api-Version: 2022-11-28`, which answers a dispatch with 204. Version 2026-03-10
  answers 200 with the run details, so moving to it means changing the success check too.
- **The schedule ends by itself.** After 2026-11-04 05:59:59 UTC the trigger still fires but sends
  nothing. Remove the cron trigger from Terraform after election day.
- **The hours live in two places.** `CRON_SCHEDULE` in `ev_api/cron.py` and the `cron` string in
  `infra/preview/main.tf` must match. `tests/verify/check_worker_cron_trigger.py` fails when they
  drift.
- **Cloudflare cron triggers fire at most once per schedule entry**, and trigger changes take up to
  15 minutes to propagate after apply.

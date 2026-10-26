# ADR-014: Early-vote ingest as a Cloudflare Workflow

**Date:** 2026-10-26
**Status:** Proposed (design for review, nothing implemented)

## Context

The early-vote sync runs in a GitHub Actions job. A run that rewrites changed days takes 25 to 45 minutes, because the loader writes to D1 through the public REST API from a runner. The owner requires that no GitHub workflow runs longer than 5 minutes, and wants the ingest on Cloudflare. The sync is paused until a replacement exists, so live early-voting data is stale.

Evidence so far:

- ADR-013 moved the schedule to a Worker cron that dispatches the Actions run. That dispatch goes away under this design.
- Spike PR #88 showed a Python Worker can fetch a statewide roster day from the Civix portal and stream-decode it: HTTP 200, no challenge, 48.0 MB decoded in about 7 s of network time, Python heap about 65 MB (measured, 2026-10-20 file).
- Cloudflare docs (read 2026-10-26) confirm Python Workflows exist (`WorkflowEntrypoint`, `step.do`, retries, `python_workflows` and `python_workers` flags), steps have no wall-clock limit, CPU is 30 s per invocation by default and configurable to 5 minutes, a Worker isolate has 128 MB of memory, and a D1 `batch()` counts as one query toward the per-invocation limit.

## What cannot be ported as written

The loader assumes a 32 GB runner. These measured or read facts rule out a line-for-line port.

| Code | Why it does not fit in 128 MB |
|---|---|
| `roster_models.load_roster_day` | Builds `set(iter_row_keys(decoded))` for the whole day (about 1M row keys) and a second set for the previous version. |
| `early_vote_roster.canonicalize_roster_csv`, `prepare_payload_bytes` | Take and return the whole 48 MB file as bytes. |
| `early_vote_d1_precompute_store.aggregate_body_from_decoded` | Parses the whole decoded file. |
| `repeat_build.build_repeats` | Peaks at 802 MB (measured on the real dry run). Cannot run in a Worker at all. |
| `voter_link` | Local only by decision. Out of scope. |

The existing code already partitions rows by signed 64-bit `row_hash` into 16 ranges (`key_ranges`, `KEY_RANGES`). That partition is the natural unit of work for a memory-bounded design.

## Candidates

Three structurally different shapes were weighed.

**A. Scatter, then diff per range (recommended).** One streaming pass per day fetches, decodes and canonicalizes the file, hashes every line, and scatters lines into small R2 segment objects grouped by hash range. A second stage runs one Workflow step per range. Each step reads only its segments, diffs against the rows D1 already holds for that range, and writes the added and removed keys. A last step checks counts, flips the version marker and collects garbage.

**B. Stage one object, re-parse per range.** The streaming pass writes one canonical object. Each range step re-reads and re-parses the whole file and keeps its slice. Simpler staging, 16 times the parse CPU per changed day. Kept as the fallback if scatter writes to R2 prove slow.

**C. Durable Object as single-writer pipeline.** A Durable Object holds a cursor in SQLite storage and advances it from alarms. It reimplements retries, durability and resume that Workflows already provide, and its alarms are capped at 15 minutes of wall time. Rejected.

| Criterion | A scatter | B re-parse | C Durable Object |
|---|---|---|---|
| Peak memory per step | one range (about 60k keys) | one range | one range |
| Parse CPU per changed day | about 2 passes | about 17 passes | about 2 passes |
| Retry and resume | built in | built in | hand built |
| Moving parts | R2 segments plus Workflow | R2 object plus Workflow | Durable Object plus alarms |
| Unchanged-day cost | one fetch and one canonical pass | same | same |

A is chosen. It keeps B as a one-line change of the staging writer if R2 segment writes disappoint.

## Decision

A Worker `scheduled` handler creates one Python Workflow instance per firing. The Workflow replaces the Actions job for the roster ingest.

### Data shapes

All step inputs and outputs are small JSON values. Step outputs are persisted by Workflows, so they hold counts, hashes, dates and object keys only. No names, no VUIDs, no CSV lines.

```
RunPlan      election_id, run_id, mode            "recent" | "sweep"
             days: list[EvDay]                    EvDay = ev_date ISO string
StagedDay    ev_date, sha256, decoded_bytes, rows, rejects_pending,
             header_cols, columns_ok,
             range_rows: list[int]                16 counts, sums to rows
             segment_prefix: str                  staging/{run_id}/{ev_date}/
             unchanged: bool                      sha256 equals stored day hash
RangeResult  ev_date, range_index, added, removed, rejected_added
DayResult    ev_date, version, rows, rejects, added, removed, status
             status: "unchanged" | "published" | "failed"
RunReport    election_id, run_id, days: list[DayResult], kv_version, failed_days
```

The domain objects already exist in `roster_models` (`RosterRow`, `RosterReject`, `DayVersion`). The roster tables do not change. One small new table, `early_vote_sync_lease`, holds the run lease described below.

### Step graph

```
plan
 └─ for each day in plan.days, in order (each day isolated in its own try):
      stage(day)            fetch + decode + canonicalize + hash + scatter to R2
      if unchanged: record and continue
      range(day, 0..15)     diff and write one hash range (sequential, D1 is single writer)
      publish(day)          count check, marker flip, gc, chunks, file row, day hash, snapshot
 finish                     aggregates, KV version bump once, report
```

Roughly 19 steps per changed day and 2 per unchanged day. A full run of 17 days is under 350 steps, far below the 10,000 default.

### Step specifications

| Step | Does | Idempotent because | Retries |
|---|---|---|---|
| `plan` | Takes the election's early-vote dates (the list the loader reads from the Civix election detail today), drops future dates, and picks `recent` (today and yesterday) or `sweep` (all days) | Pure read | 3 |
| `stage` | One portal GET, streams the envelope through `EnvelopeStreamDecoder`, canonicalizes line by line (BOM, CRLF, quoting rules of `canonicalize_roster_csv`), computes sha256 over canonical bytes, `row_hash` and `occ` per line, appends to a per-range buffer flushed to `staging/{run_id}/{date}/{range}/{seq}` at 512 KB. Compares the sha256 with `early_vote_roster_version.sha256` | Staging keys are run-scoped. A retry overwrites the same keys | 3, backoff 30 s |
| `range` | Reads its segments, validates rows with `RosterRow.model_validate`, reads the range's visible keys from D1 (`VISIBLE_PAGE_SQL`, 10k per page), computes added and removed, runs `recovery_statements` for the range first, then inserts added rows tagged `added_in = target` and marks removed rows | `recovery_statements` clears any unpublished marks for the range before every attempt, as the loader does today | 5, backoff 30 s |
| `publish` | Sums range results, checks `COUNT_VISIBLE_SQL` at `target` equals `rows + rejects`, writes the version marker last, then `gc_statements`, then writes file chunks, the file row, the precomputed aggregates, the day hash, the pointer, the R2 snapshot manifest and the day-change log | The marker flips once. A failure before the flip leaves the published version unchanged | 3 |
| `finish` | Bumps the KV `ev:{election}:version` once, only if any day published | KV write is a pure overwrite after D1 | 3 |

The order on a published day matches `_write_day_scoped` today: D1 data first, KV bump after. The ~60 s KV propagation caveat is unchanged and is unsuitable for election-night results; a faster path is out of scope.

### One load path, not two

`load_roster_day` is split into a per-range function and a day-level publish function. The existing whole-day function becomes their composition and keeps its signature, so the loader, the CLI and the stateful Hypothesis machine keep working. The Workflow calls the same range and publish functions. There is one implementation of the diff and one test suite for it. This is the migration of callers to the new split, not a second code path.

The previous version's keys come from D1 (the visible rows in the range) instead of re-reading the base file from R2. That removes a second file pass and the "base file is gone" full-replace case, since D1 is the source of truth for what readers see.

### Store adapters

`RosterRowsStore` is already a two-method protocol (`_query_rows`, `_statements`). A `D1BindingStore` implements it over the Worker binding. It ignores the database id argument, resolves the binding name from `EV_D1_BINDINGS`, runs `prepare(sql).bind(...).all()` for reads and `db.batch([...])` for writes. An `R2SegmentStore` wraps the R2 binding for put, list, get and delete. Both live in the Worker package. The loader modules keep using `CloudflareEarlyVoteStore`, so the CLI paths are untouched.

### Cron and schedule

The Worker `scheduled` handler keeps ADR-013's `should_dispatch` hours (10:00, 17:00 and 22:00 Central, ending 11:59 PM on 2026-11-03) and creates the Workflow instead of calling GitHub. The instance id is `ev-{election}-{yyyymmddhh}`, so a duplicate firing for the same hour should be rejected by Workflows (to be confirmed in R6). Firings at 10:00 and 17:00 run `recent`. The 22:00 firing runs `sweep`.

### Failure and resume model

| Event | Outcome |
|---|---|
| Portal error or non-200 in `stage` | Step retries with backoff. After the limit, the day is `failed`, the run continues with other days, the report lists it |
| Range step crashes mid-write | The retry runs `recovery_statements` for that range, then rewrites it. Readers still see the previous version |
| Crash after the marker flips but before gc | The next run's recovery pass for the day clears nothing visible, gc runs again |
| Whole Workflow dies | Workflows resumes from the last completed step. A new firing creates a fresh instance and recomputes from the portal |
| Two firings overlap | Different instance ids can run at once. D1 serializes writes, but two writers on the same day would race, so `plan` records a lease row (`election_id`, `run_id`, `expires_at`) and a second instance exits when a live lease exists |
| Bad row in a file | Quarantined to `early_vote_roster_reject` as today; counts reported |

### Privacy

Workflow step outputs and instance state persist (3 days on Free, 30 on Paid). Outputs carry no names or VUIDs by construction, and a test asserts it. R2 staging objects do contain voter names (public record, but still voter data). They go in a private bucket, are deleted by the `publish` step after the marker flips, and carry a 1-day lifecycle rule as a backstop. Logs carry counts only, per the repo rule. The Workflow's `default_retention` is set to the shortest allowed value.

## What stays off the Workflow

- **Repeat build.** 802 MB peak. Options in order of preference: keep it as a short Actions job (about 90 s measured) dispatched by `finish`; later rewrite it as set-based SQL in D1 partitioned by VUID range. Decision for the owner.
- **Geography load.** One-time and idempotent. Not part of the recurring run.
- **Voter link.** Local by decision.
- **GitHub Pages CSV publication.** Stays until Quick Results is live. It reads the Workflow's output via the existing publish path.

## Risks and the second spike

| # | Risk | Test |
|---|---|---|
| R1 | Streaming canonicalize and hashing exceeds 128 MB or the CPU limit in Pyodide | Second spike |
| R2 | The canonical sha256 differs from the one the loader stores today | Second spike compares against the stored hash for 2026-10-20 |
| R3 | R2 scatter writes (about 100 puts per day) or reads are slow | Second spike times them |
| R4 | D1 binding write throughput is lower than the REST path | Measure rows per second on a single range |
| R5 | Terraform cannot declare the Workflow binding or class | Read provider docs before building. If unsupported, escalate; a wrangler-only resource breaks the "Terraform-managed" rule |
| R6 | Python Workflows beta quirks (loops of steps, rejecting a duplicate instance id, retry config as dicts) | Hello-world Workflow with 300 steps under Terraform |
| R7 | SQLModel validation inside a step costs too much CPU per range | Measure in the spike; fall back to the plain column validator used by the Worker today |
| R8 | The portal rate limits repeated pulls from Cloudflare IPs | Pace at 1 s or more, run `recent` only most of the time |
| R9 | Ingest writes slow read-API latency (D1 is single-threaded) | Measure p95 of `/elections` during a range step |
| R10 | Step outputs leak row content | Hypothesis test over every step output type |

**Second spike scope.** Extend the temporary spike route (and nothing else) to stream-canonicalize the real 2026-10-20 file, compute the sha256 and per-line `row_hash`, scatter to a staging R2 bucket, and report counts, timings and whether the sha256 equals `early_vote_roster_version.sha256` for that date. This needs one Terraform change (a staging R2 binding and bucket) and goes through the normal plan and apply gates. In parallel, deploy a Hello-world Python Workflow with a 300-step loop to settle R6 and R5.

## Cutover and rollback

1. Second spike passes R1, R2, R3, R5, R6.
2. Build the split of `load_roster_day`, the adapters and the Workflow behind `dry_run`. The Workflow computes hashes and diffs and writes nothing. Compare its row counts and sha256 for three days with the stored state.
3. Enable writes for one past day with a small change set. Verify counts, the visible rows against the previous version, and that the read API serves the same bytes.
4. Enable the full run on the cron. The Actions workflow stays in the repo, disabled.
5. After 2026-11-03, remove the cron trigger, the Actions sync and the dispatch code in `ev_api/cron.py`.

Rollback at any step is `gh workflow enable early-vote-sync.yml` plus pointing the cron back at dispatch. Every write is versioned and the marker is last, so a half-finished Workflow run never changes what readers see.

## Open decisions for the owner

1. Repeat build placement (short Actions job now, SQL in D1 later).
2. `recent` versus `sweep` policy. Skipping older days relies on late portal edits being rare. The 22:00 sweep bounds the exposure to one day.
3. Whether to drop the D1 copy of the raw file (about 64 MB written per changed day) and serve roster CSV from R2. Not part of this change.
4. Staging bucket name and retention, and how failures are reported (Workflow status only, or an email or alert).

## Consequences

- No GitHub job exceeds a few minutes. The sync becomes Cloudflare-native and Terraform-managed.
- The diff logic has one implementation, covered by the existing stateful tests.
- D1 write throughput, not network round trips, becomes the limit on large changed days. The Workflow has no wall-clock limit, so slow is acceptable and visible.
- The Workers Python runtime and Python Workflows are beta, so the schedule through 2026-11-03 depends on R5 and R6 passing. The Actions workflow stays available as the rollback.

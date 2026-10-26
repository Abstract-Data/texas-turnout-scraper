# ADR-014: Early-vote ingest as a Cloudflare Workflow

**Date:** 2026-10-26
**Status:** Proposed (revision 4, after three independent reviews and the owner's decisions of 2026-10-26). Design only, nothing is implemented.

## Context

The early-vote sync runs in a GitHub Actions job. A run that rewrites changed days takes 25 to 45 minutes, because the loader writes to D1 through the public REST API from a runner. The owner requires that no GitHub workflow runs longer than 5 minutes, and wants the ingest on Cloudflare. The sync is paused until a replacement exists, so live early-voting data is stale.

Evidence so far:

- ADR-013 moved the schedule to a Worker cron that dispatches the Actions run. Under this design the cron creates a Workflow instead.
- Spike PR #88 showed a Python Worker can fetch a statewide roster day from the Civix portal and stream-decode it: HTTP 200, no challenge, 48.0 MB decoded in about 7 s of network time, Python heap about 65 MB (measured, 2026-10-20 file).
- Cloudflare docs (read 2026-10-26) confirm Python Workflows exist (`WorkflowEntrypoint`, `step.do`, retries, `python_workflows` and `python_workers` flags), steps have no wall-clock limit, CPU is 30 s per invocation by default and configurable to 5 minutes, an isolate has 128 MB, and a D1 `batch()` counts as one query toward the per-invocation limit. Step output size, subrequest counts for binding calls and concurrency limits are not yet confirmed for this design (risk R12).

Revision 1 was blocked by an independent review. Revision 2 was reviewed again and blocked narrowly on eight gaps. Revision 3 was reviewed a third time; that review blocked narrowly again on a short list of edits and said the second spike could start. All three sets of findings are folded in below and listed under "Review findings". The owner's answers to the open decisions are recorded under "Decisions".

## What cannot be ported as written

The loader assumes a 32 GB runner.

| Code | Why it does not fit in 128 MB |
|---|---|
| `roster_models.load_roster_day` | Holds `set(iter_row_keys(decoded))` for the whole day (about 1M keys) and a second set for the previous version. |
| `early_vote_roster_rows.keyed_lines` | Assigns `occ` with a dict over every distinct `row_hash` in the day. |
| `early_vote_roster.canonicalize_roster_csv`, `prepare_payload_bytes` | Take and return the whole 48 MB file. |
| `early_vote_sync._keep_replaced_version`, `early_vote_day_changes.diff_rosters`, `roster_row_count` | Join the old file out of D1 chunks (a 48 MB `b"".join`) and build two VUID dictionaries. |
| `aggregate_body_from_decoded` | Parses the whole decoded file. |
| `repeat_build.build_repeats` | 802 MB peak (measured on the real dry run). |

Rows are already partitioned by signed 64-bit `row_hash` into 16 ranges (`key_ranges`). That partition is the unit of memory-bounded work.

## Candidates

**A. Scatter, then diff per range (recommended).** One streaming pass per day writes two things to R2: the canonical file in file order, and small per-range segment objects. One Workflow step per range then diffs only its segments against the rows D1 holds for that range.

**B. One staged object, re-parse per range.** No segments. Each range step re-reads the canonical file and keeps its slice. About 17 parses of the day instead of 2. Kept as the fallback if segment writes to R2 are slow.

**C. Durable Object single-writer pipeline.** Reimplements retries and resume that Workflows provide, and its alarms are capped at 15 minutes of wall time. Rejected.

| Criterion | A scatter | B re-parse | C Durable Object |
|---|---|---|---|
| Peak memory per step | one range (about 60k keys) | one range | one range |
| Parse CPU per changed day | about 2 passes | about 17 passes | about 2 passes |
| Retry and resume | built in | built in | hand built |
| Moving parts | R2 canonical object and segments, Workflow | R2 canonical object, Workflow | Durable Object, alarms |

## Decision

A Worker `scheduled` handler creates one Python Workflow instance per firing. The Workflow replaces the Actions job for the roster ingest. Candidate A is built on the existing safety rules: D1 data first, the version marker flipped only after the rows are complete, the day hash written last as the commit point, and the KV bump after D1.

### Ordering rule (corrected)

The "unchanged" check reads `early_vote_day.sha256`, the same table `stored_hash` reads today. That row is written after everything else for the day, so a crash anywhere earlier is retried as changed. The roster version marker is not the commit point and must not be used for the unchanged check.

### Data shapes

Step inputs and outputs are small JSON values. Outputs are persisted by Workflows, so they hold counts, hashes, dates and R2 keys only. No names, no VUIDs, no CSV lines.

```
RunPlan      election_id, run_id, mode ("recent" | "sweep"), days: list[ev_date]
StagedDay    ev_date, file_id (sha256 + run_id, minted once here), raw_sha256,
             sha256 (canonical), decoded_bytes, rows,
             header_cols, empty: bool
             canonical_key              staging/{run_id}/{ev_date}/file.csv
             range_segments: list[int]  exact segment count per range, 16 entries
             range_rows: list[int]      16 counts, sum is rows plus rejects
             county_method_counts       at most 254 x 2 counts, no PII
             unchanged: bool            sha256 equals early_vote_day.sha256
RangeResult  ev_date, range_index, added, removed, rejects_added, rejects_removed
DayResult    ev_date, version, rows, rejects, distinct_vuids, added, removed, status
             status: "unchanged" | "published" | "skipped_empty" | "failed"
RunReport    election_id, run_id, days: list[DayResult], kv_version, failed_days
```

### Step graph

```
plan
 for each day in plan.days, in order, each day isolated (a failed day is reported, the run goes on):
   stage(day)           one pass: fetch, decode, canonicalize, hash, write canonical object + segments
   if empty:            skipped_empty, never published
   if unchanged:        record and continue
   files(day)           old and new R2 snapshots, file chunks, file row, aggregates (as today, before rows)
   recover(day)         day-level recovery, and retire all rows when a full replace is needed
   range(day, 0..15)    diff and write one hash range (sequential, D1 is a single writer)
   flip(day)            count check, version marker, change log, gc
   commit(day)          early_vote_day hash upsert, pointer, repoint, staging and candidate cleanup
 finish                 finalize_published_roster, KV version, hash and manifest keys, precompute check
```

About 22 steps for a changed day, 2 for an unchanged one. Under 400 steps for a full run, far below the 10,000 default.

### Step specifications

| Step | Does | Idempotent because | Retries |
|---|---|---|---|
| `plan` | Takes the election's early-vote dates, drops future dates, picks `recent` (today and yesterday) or `sweep`. Takes the run lease (below) and writes the run's status row (decision 8) | Pure read, then a conditional lease write and an upsert keyed by `run_id` | 3 |
| `stage` | Deletes any prior objects under its run-scoped prefix, then one portal GET. Streams the envelope through `EnvelopeStreamDecoder`, canonicalizes incrementally, hashes the canonical bytes, writes the canonical object by multipart upload, and writes `(row_hash, cells)` records to per-range segments flushed at 512 KB. Counts rows per county and method | The prefix is cleared first and the exact segment counts are returned, so a retry never reads stale segments | 3, backoff 30 s |
| `files` | Streams the canonical object into the R2 snapshot object (gzip) for the new version, snapshots the replaced version, writes file chunks (32 KiB base64, about 1,465 per day, batched), the file row under the `file_id` minted in `stage`, and the precomputed aggregates plus their meta row from the staged counts. It does not write the snapshot manifest entry (see `flip`) | Keys are content-addressed or replaced by `file_id`, as today. A retry reuses the same `file_id`, so it rewrites instead of orphaning | 3 |
| `recover` | Reads the day's current version row once and returns `V`, `target = V + 1`, `prior_sha256` (the sha on the VERSION row, which is what the stored rows actually are, not `early_vote_day.sha256`) and `full_replace`. `full_replace` is true when the rows visible at `V` plus rejects do not equal the version row's counts (the integrity check that replaces today's "base file is gone" case, since the Workflow has no base file). Runs the day-level `recovery_statements`, retires all rows when `full_replace`, and records `(run_id, ev_date, file_id, prior_sha256, target)` in `early_vote_sync_day_run`. Later steps use these values and never re-read the version | Same statements the loader runs first today. A retry returns the same values because nothing has flipped | 3 |
| `range` | Takes `V`, `target`, `full_replace` from `recover`. Reads its segments, computes `occ` with a dict limited to this range, reads the range's current keys from D1 (empty when `full_replace`), diffs, validates and inserts added rows tagged `added_in = target`, marks removed rows, and records the changed VUIDs in `early_vote_sync_candidate`, whose key is `(run_id, ev_date, range_index, id_voter)`. Starts by clearing its own range's unpublished marks and its own candidate rows. Renews the lease every 100 batches | Per-range recovery and candidate cleanup make a retry safe without touching other ranges | 5, backoff 30 s |
| `flip` | Checks visible rows plus rejects at `target` equal the staged count. If every range reported zero added and zero removed, only restamps `file_id` and sha (the same-keys case in `load_roster_day`) and does not bump the version. Otherwise writes the version marker at `target`. Then computes the VUID-level figures from `early_vote_sync_candidate` (change log keyed on `prior_sha256`, `COUNT(DISTINCT id_voter)` for the manifest), appends the manifest entry with `record_version` semantics (read the manifest, skip if the sha is present, append; the object already exists from `files`; the lease prevents concurrent writers; the restamp case appends too, as today), and runs `gc_statements` per range | The marker flips once. A failure before it leaves the published version unchanged. The change-log insert is `INSERT OR IGNORE` | 3 |
| `commit` | Upserts `early_vote_day` (the commit point) and updates the pointer, repointing as `_write_day_scoped` does. It deletes nothing that a reader could need: no old file, no precompute rows. It deletes the staging prefix and this run's `early_vote_sync_candidate` rows for the day | Plain upserts. Staging deletes are retry-safe | 3 |
| `finish` | A reimplementation of `finalize_published_roster` against Worker bindings, without `backfill_precomputed_aggregates` (the Python version decodes every file that lacks aggregates, which cannot run here). On every run, whether or not a day published, it checks that the pointer's `file_id` equals the latest stored day's `file_id`, that the KV hash equals that day's sha, and that the file is complete (chunks and the aggregate meta row, the condition `choose_published_file` requires), and repairs the difference. It uses the latest stored day, not the latest processed day. The manifest digest is computed over every stored day's hash, read from D1; this is a deliberate change from `run_backfill`, which fed `backfill_manifest_digest` only the days processed in that run, and it makes `recent` and `sweep` runs agree. It bumps `ev:{election}:version`, writes the KV hash and manifest keys, and only then deletes superseded files using the keep-set below. Deletes any `early_vote_sync_candidate` rows left by this run's failed days. Updates the run's status row. Releases the lease | KV writes are overwrites after D1. Deletes use a keep-set, so a repeat is a no-op | 3 |

The ~60 s KV propagation caveat is unchanged and is unsuitable for election-night results; a faster path is out of scope.

### Details that must match the current loader

- **Scatter keeps `occ` correct.** Identical lines have identical cells, so they have the same `row_hash` and land in the same range. `stage` writes only `(row_hash, cells)`. The `range` step numbers duplicates (`occ`) with a dict limited to its range, which gives the same numbers as `keyed_lines`. The header seed is hashed first, so a header change re-keys every row.
- **Streaming canonicalizer.** The reference is `csv.reader` over `decode("utf-8-sig")` with `csv.writer(lineterminator="\n")`. The streaming version must use an incremental `utf-8-sig` decoder, split input on `\n` only while keeping terminators (so quoted newlines survive), treat only `[]` rows as blank (a whitespace-only line is not blank), return `b""` for an empty file, and header plus `\n` for a header-only file. A differential Hypothesis test compares it with `canonicalize_roster_csv` on generated files.
- **Range-bounded keys.** The key read uses a new keys-only, range-bounded query on both the row table and the reject table. The current `VISIBLE_PAGE_SQL` has no range bound, returns names, and ignores rejects, so it is not reused.
- **Per-range recovery.** Add range-scoped variants of `recovery_statements`, `retire_statements` and `gc_statements`. The day-level versions stay for the loader.
- **Same keys, different bytes.** When the key sets are equal but the file differs (reordered lines), `load_roster_day` restamps `file_id` and sha without a new version. `flip` keeps that behavior.
- **Whole-file side effects.** The day-change log and the snapshot manifest `rows` count are distinct-VUID figures. The owner accepted documented small differences from the old code (decision 6). They are computed in D1 from accepted rows only: distinct `id_voter` for the count, and for a VUID whose duplicate rows differ, the lowest county and then method. VUIDs from rejected lines are not counted, and VUID `0000000000` is skipped as today. To avoid full scans, each `range` step records the VUIDs of its added and removed rows in `early_vote_sync_candidate`, and `flip` checks presence at `V` and at `target` only for those candidates. This needs an index on `early_vote_roster_row (day, id_voter)`, to be measured in the scratch database (R4, R15). The change log is written with the sha the day was diffed against (`prior_sha256`, read from the version row in `recover`), so a second portal change before the retry cannot mislabel a transition. The SQL is differentially tested against `diff_rosters` and `roster_row_count` on files without rejects or conflicting duplicates, and the expected differences are asserted on files with them (R11).
- **Empty portal files.** `stage` reports `skipped_empty` when the file has no data rows and the day is never published, matching `run_backfill`.
- **No day-level early exit.** The same-keys short-circuit of `load_roster_day` lives only inside `flip` (a restamp). It is never a reason to skip `files` or `commit`.
- **Staging objects.** The canonical object is written by multipart upload with fixed 8 MiB parts (about 6 parts, 8 to 16 MB of heap). Open multipart uploads are not objects, so a retried `stage` cannot find them to delete. A lifecycle rule aborts incomplete multipart uploads after 1 day. Range segments are immutable objects of about 512 KB each, about 115 per day.

### One load path

`load_roster_day` is split into a per-range function and a day-level publish function. The existing whole-day function becomes their composition and keeps its signature, so the CLI, the loader and the stateful Hypothesis machine keep working. The Workflow calls the same range and publish functions. The split functions move to a module with no sqlmodel import, because `roster_models` is loader-only and the Worker must not import it (AGENTS.md, ADR-009). The owner decided to allow SQLModel in the Worker (decision 5), so row validation uses the same models. The spike measures cold start and CPU (R7); if either is bad, a plain Worker-safe validator with a drift test replaces it.

### Store adapters

`RosterRowsStore` is already a two-method protocol (`_query_rows`, `_statements`). A `D1BindingStore` implements it over the Worker binding. It ignores the database id argument, resolves the binding name from `EV_D1_BINDINGS`, runs `prepare(sql).bind(...).all()` for reads and `db.batch([...])` for writes. An `R2StagingStore` wraps the R2 binding for multipart put, put, list, get and delete. Both live in the Worker package. The loader keeps `CloudflareEarlyVoteStore`.

### Cron and schedule

The Worker `scheduled` handler keeps ADR-013's `should_dispatch` hours (10:00, 17:00 and 22:00 Central, ending 11:59 PM on 2026-11-03) and creates the Workflow instead of calling GitHub. The instance id is `ev-{election}-{yyyymmddhh}`, which should reject a duplicate firing for the same hour (to be confirmed in R6) and also blocks a manual re-run in that hour, so a manual run uses a different suffix. The 10:00 and 17:00 firings run `recent`. The 22:00 firing runs `sweep`.

### Lease

Two instances writing the same day would race. `plan` takes a lease with one conditional statement: `INSERT ... ON CONFLICT(election_id) DO UPDATE SET holder = excluded.holder, expires_at = excluded.expires_at WHERE early_vote_sync_lease.expires_at < unixepoch()`, then checks that one row changed. D1 serializes writes, so the acquire is race-free. The clock is D1's `unixepoch()`, never Worker time. Renewal is `UPDATE ... SET expires_at = ... WHERE holder = :me AND expires_at > unixepoch()`, and zero rows changed aborts the instance. The TTL is 10 minutes, every step renews it, and the long steps (`stage`, `files`, `range`) renew inside the step every 100 batches. `finish` releases it. A terminated instance never releases, so the lease expires and blocks the next firing for at most 10 minutes, with no data risk. Paths that do not take the lease (the Actions workflow, local CLI runs, `early_vote_roster_rows_backfill`) can still race. Cutover step 4 disables the Actions workflow, and the RUNBOOK says local loader runs are not used while the Workflow is live. Making those paths take the lease would be new work and is not planned. The lease table is `early_vote_sync_lease` in each election DB, added to the schema list with a drift test. The acquire clause also matches `OR early_vote_sync_lease.holder = excluded.holder`, with the holder derived from the Workflow instance id, so a `plan` retry after a lost response does not lock itself out.

### Superseded files (keep-set)

`early_vote_file` is keyed by election and file id and has no day. A crashed run can leave a flipped version pointing at a file that `early_vote_day` does not yet reference, so deletion must be conservative. `finish` deletes a file only when it is not in the keep-set: the pointer's file, every file whose sha equals the current KV hash, every stored day's `file_id`, every `file_id` recorded in `early_vote_sync_day_run` for the live run, and every `file_id` named by a roster version row. This is the same rule `_delete_stale_files` applies today (it keeps the KV-hash files), extended with the version rows. Deleting only after the KV bump closes the window in which the pointer names the new file while KV still holds the old hash and readers would see an incomplete file. Precompute rows go with their file, using `delete_precompute_statements`.

### Failure and resume model

| Event | Outcome |
|---|---|
| Portal error or non-200 in `stage` | Retries with backoff. After the limit the day is `failed`, other days continue, the report lists it |
| Range step crashes mid-write | The retry clears its own range's unpublished marks and rewrites. Readers still see the previous version |
| Crash after `flip` before `commit` | `early_vote_day` still holds the old hash, so the next run treats the day as changed and finishes it. The roster version and the day row agree after `commit` |
| Whole Workflow dies | Workflows resumes from the last completed step. A new firing starts fresh from the portal |
| Overlapping firings | The lease makes the second instance exit |
| Bad row in a file | Quarantined to `early_vote_roster_reject`; counts reported |

### Privacy

Workflow step outputs and instance state persist (3 days on Free, 30 on Paid). Outputs carry no names or VUIDs by construction, and a Hypothesis test asserts it for every output type. R2 staging objects hold voter names (public record, still voter data). They go in a private bucket, are deleted by `commit`, and a 1-day lifecycle rule backs that up. Logs carry counts only. `default_retention` on the Workflow binding is set as short as allowed.

## What stays off the Workflow

- **Repeat build.** Paused until after 2026-11-03 (decision 1). Nothing reads the repeat tables yet. It is rebuilt later as set-based D1 SQL partitioned by VUID range. The `GITHUB_DISPATCH_TOKEN` binding and the dispatch code are removed at cutover.
- **Geography load.** One-time and idempotent. A new election needs a new per-election D1 and a binding in Terraform, which is an owner-approved change and not something the Workflow does.
- **Voter link.** Local by decision.
- **GitHub Pages CSV.** `data-refresh.yml` is a separate workflow and is unaffected. This ADR does not claim a link between it and the Workflow.

## Review findings

Revision 1 to 2 (first review, all resolved):

| # | Finding | Fix |
|---|---|---|
| 1 | Marker flipped before chunks, file row, aggregates, day hash; unchanged check read the wrong table | Original order restored; unchanged check reads `early_vote_day.sha256` |
| 2 | Scatter cannot produce the ordered canonical file | `stage` also writes the canonical object in file order |
| 3 | `occ` needs a whole-day dict in `stage` | `occ` computed per range |
| 4 | `recovery_statements` not range-scoped | Range-scoped variants plus one day-level `recover` step |
| 5 | Rejects invisible to the diff, key read unbounded and returned names | Keys-only, range-bounded query on both tables |
| 6 | Change log and manifest counts are whole-file VUID figures | See revision 2 to 3, items 3 and 4 |
| 7 | `finish` incomplete | Specified |
| 8 | Streaming canonicalizer diverges | Behaviors specified, differential test |
| 9 | Lease race and TTL unspecified | See revision 2 to 3, item 5 |
| 10 | Stale segments on a retry | Prefix cleared, exact counts, multipart abort rule |
| 11 | Worker would import loader-only sqlmodel | Decision 5 allows SQLModel in the Worker, measured in the spike |
| 12 | Approvals and Terraform changes unlisted | Listed |

Revision 2 to 3 (second review, narrow BLOCK, all folded in):

| # | Finding | Fix |
|---|---|---|
| 1 | `file_id` undefined; a retry mints a new id and orphans chunks; `finish` would decode every file in a 128 MB isolate | `file_id` minted once in `stage`; `files` writes the aggregate meta row; orphan files are deleted by `finish` with a keep-set (revision 3 had `commit` do it, see the third review); `finish` checks state instead of the full backfill |
| 2 | Crash between `flip` and `commit`: readers consistent, but the change log can mislabel and `V` and `target` can be re-read after the marker moved | `recover` returns `V` and `target`; the short-circuit lives only in `flip`; the log carries `prior_sha256` |
| 3 | `files` cannot compute the manifest `rows` count before rows exist | The manifest entry is appended in `flip` from D1 |
| 4 | The set-based SQL cannot reproduce last-row-wins, rejects differ, no `id_voter` index | Decision 6 accepts documented differences; candidate-set check; index in the approvals |
| 5 | Lease needs a renewal rule, in-step renewal, a clock, and disabling other writers | Specified |
| 6 | A terminated instance skips `finish`; the manifest digest depends on the mode | `finish` reconciles every run; digest over all stored days |
| 7 | Multipart upload sizes and abandoned uploads | Fixed 8 MiB parts, abort-incomplete lifecycle rule |
| 8 | Cutover write test is a production write not listed | Scratch D1 first; the preview write needs explicit approval |

Revision 3 to 4 (third review, narrow BLOCK, spike cleared to start):

| # | Finding | Fix |
|---|---|---|
| 1 | `commit` deleted the old file; readers see an incomplete file until the KV bump, longer if the instance dies. No mechanism existed to find orphans | Deletion moved to `finish` after the KV bump, with an explicit keep-set and `early_vote_sync_day_run` bookkeeping |
| 2 | `prior_sha256` came from `early_vote_day`, which can differ from the version row after a crash | Carried from the version row in `recover` |
| 3 | `finish` could not call `finalize_published_roster` as written; reconcile test omitted file completeness; the manifest digest change was unflagged | `finish` is a stated reimplementation; completeness added; digest change called out |
| 4 | Lease self-lockout on a `plan` retry; `range` had no renewal | `OR holder = excluded.holder`; renewal in `range` |
| 5 | Candidate table keying and cleanup; manifest entry ownership and the restamp case | Key and cleanup specified; `flip` owns the manifest with `record_version` semantics |
| 6 | Status row writer missing; local loader paths and the lease; "six gaps" count | Writers named; the claim is dropped; wording fixed |
| 7 | No risk rows for the above, and the `full_replace` trigger | R17 to R21 added; the trigger is the version-row integrity check |

## Approvals needed

- Terraform: a Workflow binding and class, a private staging R2 bucket with lifecycle rules (1-day expiry and abort-incomplete-multipart), a scratch D1 database for the spike and tests, the cron trigger now creating a Workflow, `limits.cpu_ms` for the Worker. Apply is the owner's, through the existing gates.
- Schema changes on the preview election DB: the `early_vote_sync_lease`, `early_vote_sync_candidate`, `early_vote_sync_day_run` and sync-status tables, and an index on `early_vote_roster_row (day, id_voter)`. These go in the schema list with drift tests and are applied by the loader's normal idempotent schema step.
- Cutover step 3 writes one past day to the preview D1. It needs the owner's explicit approval at that time.
- Cutover step 4 re-enables a scheduled write path.

## Risks and the second spike

| # | Risk | Test |
|---|---|---|
| R1 | Streaming canonicalize, hash and scatter exceed memory or CPU in Pyodide | Second spike reports CPU ms (Workers tail or analytics) and peak memory |
| R2 | The canonical sha256 differs from the loader's | The spike pulls the same raw bytes locally, runs `canonicalize_roster_csv`, and compares the raw and canonical hashes. The stored hash alone is a weak check because the portal file may have gained records |
| R3 | R2 canonical upload (about 48 MB) and about 100 segment writes are slow | Second spike times them |
| R4 | D1 binding write throughput is below the REST path | Write one range through a binding to a scratch database and time it |
| R5 | Terraform cannot declare the Workflow binding or class | Read provider docs first; if unsupported, escalate, since a wrangler-only resource breaks the Terraform-managed rule |
| R6 | Python Workflows beta quirks: step loops, duplicate instance ids, retry config as dicts, catching a failed day and continuing | Hello-world Workflow with a 300-step loop under Terraform |
| R7 | SQLModel import and validation cost in Pyodide | Measure; fall back to a Worker-safe validator |
| R8 | The portal rate limits repeated pulls from Cloudflare | Pace at 1 s or more, `recent` most of the time |
| R9 | Ingest writes slow read-API latency (D1 is single-threaded) | Measure p95 of `/elections` during a range step |
| R10 | Step outputs leak row content | Hypothesis test over every output type |
| R11 | Set-based VUID SQL differs from `diff_rosters` and `roster_row_count` | Differential test on generated rosters |
| R12 | Step output size, per-invocation query and subrequest counts for D1 and R2 calls, Workflow concurrency | Read the limits pages; assert in the spike |
| R13 | Bound-parameter size (4000-row JSON payload, 1 to 2 MB) accepted through the Python binding | Spike write test |
| R14 | Streaming gzip of 48 MB to R2 inside the CPU limit | Spike |
| R15 | First load of a new election: about 1M inserts per day | Time on the scratch database |
| R16 | DST boundary at 2026-11-01 for the 22:00 Central firing | Covered by `cron.py` tests; recheck in the Workflow cron |
| R17 | A crashed run leaves a flipped file that no day row references; the keep-set deletes too much or too little | Crash tests at each step boundary on the scratch DB; assert readers always get a complete file |
| R18 | Lease self-lockout, expiry during a long `range` | Retry test for `plan`; first-load timing (R15) against the 10-minute TTL |
| R19 | Candidate table grows or leaks across runs | Cleanup in `commit`/`finish`; row-count assertion after a run |
| R20 | Reimplementing `finalize_published_roster` against Worker bindings diverges from the Python version | Differential test of the pointer, KV keys and the manifest digest against the loader on a fixture DB |
| R21 | The version-row integrity check (`full_replace`) fires wrongly or never | Tests with corrupted visible counts, missing rows and a missing version row |

**Second spike scope.** Extend the temporary spike route to stream-canonicalize the real 2026-10-20 file, hash it, write the canonical object and range segments to a staging R2 bucket, and report counts, timings, CPU and the raw and canonical sha256. Run `canonicalize_roster_csv` locally on the same raw bytes and compare. Write one range through a D1 binding to a scratch database and compare `occ` assignment with `keyed_lines`. This needs one Terraform change (staging R2 bucket and binding, a scratch D1) through the normal gates. In parallel, deploy a Hello-world Python Workflow with a 300-step loop to settle R5, R6 and R12.

## Cutover and rollback

1. Second spike passes R1 to R6 and R12 to R14.
2. Build the split of `load_roster_day`, the adapters and the Workflow behind `dry_run`: it computes hashes and diffs and writes nothing. Compare row counts and sha256 for three days with the stored state.
3. Run the whole pipeline against a scratch D1 first (range writes, `occ`, timings, the VUID checks). Then, with the owner's explicit approval, enable writes for one past day on the preview D1 with a small change set. Verify counts, the visible rows against the previous version, and that the read API serves the same bytes.
4. Enable the full run on the cron. The Actions workflow stays in the repo, disabled. Local loader runs are not used while the Workflow is live.
5. After 2026-11-03, remove the cron trigger, the Actions sync and the dispatch code in `ev_api/cron.py`.

Rollback at any step is `gh workflow enable early-vote-sync.yml` plus pointing the cron back at dispatch. Every write is versioned and the day hash is last, so a half-finished Workflow run never changes what readers see.

## Decisions (owner, 2026-10-26)

| # | Question | Decision |
|---|---|---|
| 1 | Repeat build | Pause until after 2026-11-03 |
| 2 | Which days per run | Recent days each run, full sweep at the 22:00 firing |
| 3 | The D1 copy of the raw file | Keep for now, revisit after 2026-11-03 |
| 4 | Staging retention | Private bucket, delete at commit, 1-day lifecycle plus abort-incomplete-multipart |
| 5 | Row validation in the Worker | Allow SQLModel in the Worker and measure it in the spike; fall back to a plain validator if cold start or CPU is bad. The AGENTS.md rule is updated with the implementing PR |
| 6 | VUID-level figures (change log, manifest `rows`) | Accept documented small differences |
| 7 | First write test | Scratch D1 first, then one past day on the preview D1 with explicit approval |
| 8 | Failure reporting | A status row per run in D1 and an authenticated route showing recent runs. Counts and failed days only, no PII |

## Consequences

- No GitHub job exceeds a few minutes. The sync becomes Cloudflare-native and Terraform-managed.
- The diff logic has one implementation, covered by the existing stateful tests.
- D1 write throughput, not network round trips, becomes the limit on large changed days. The Workflow has no wall-clock limit, so slow is acceptable and visible.
- Python Workflows and the Workers Python runtime are beta, so the schedule through 2026-11-03 depends on R5 and R6 passing. The Actions workflow stays available as the rollback.

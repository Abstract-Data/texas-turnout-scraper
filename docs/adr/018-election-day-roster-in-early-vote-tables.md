# ADR-018: Election Day rosters in the early-vote tables

**Date:** 2026-11-09
**Status:** Proposed. Spec only: nothing below is built, and D1 is unchanged.

## Context

`ev-53815` holds 17 roster days for the 2026 November general, 2026-10-14 to 2026-10-30: 7,641,425
live rows (7,448,344 `IN-PERSON`, 193,081 `MAIL-IN`). It has nothing for 2026-11-03.

The loader cannot reach Election Day today:

- `find_election_ev_dates` returns only the portal's `early_voting_dates`, so
  `plan_backfill_load_dates` never sees the election date.
- `pull_statewide_roster_file` requests `EVR_STATEWIDE` and expects a CSV. Election Day is
  `EVR_STATEWIDE_ELECTIONDAY`, a ZIP.
- The Container schedule ended at 23:59 Central on 2026-11-03 (`LAST_RUN_MS`).

The owner decided that Election Day goes in the same tables as early voting, that it is served from
the existing roster routes, and that every calculation route includes it.

### What the file looks like (measured 2026-11-09, `isCertified=false`)

The portal publishes the report now. These results come from a dry run of the real file through the
loader's own parsers, writing nothing to D1.

| Check | Result |
|---|---|
| ZIP | 37.9 MB, with `STATEWIDE_VOTER_INFO.csv` (132 MB) and `STATEWIDE_POLLING_PLACE_INFO.csv` (5,598 rows) |
| Rows | 2,073,832: 2,052,018 `IN-PERSON`, 21,814 `MAIL-IN` |
| Distinct VUIDs | 2,073,828, all 10 digits |
| Header | `COUNTY_NAME,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT` |
| Stored early-vote header | `COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT` |
| Unmodified header | Rejected: `roster header is missing columns: COUNTY` |
| Header with `COUNTY_NAME` renamed | Accepted; payload kind `statewide_roster` |
| Row validation | 2,073,832 valid, 0 rejects, 2,073,832 distinct row keys |
| Canonical size | 115.8 MB, about 3,534 chunks and 519 row-insert statements |
| Aggregate body | Matches the row counts; 254 counties, every one resolves to a FIPS code |
| County spellings | Identical to the 254 in `geo_precinct_alias` |
| Precinct pairs | 2,072,933 of 2,073,832 rows (99.96%) already have an alias; 831 pairs are new |
| Portal county table | 2,057,366 in person on the date, 5,348 (0.26%) more than the file |
| Peak memory of the dry run | 1.5 GB |

## Decision

### 1. Election Day is one more dated roster

The file is stored with `ev_date = '2026-11-03'` (`day` 739923) in `early_vote_day`, `early_vote_file`,
`early_vote_chunk`, `early_vote_file_aggregate`, `early_vote_county_aggregate`,
`early_vote_roster_row`, `early_vote_roster_version` and `early_vote_first_seen`. No table is added,
and no key changes. `ev_date` and the CSV's `EV_DATE` keep their names, so they now mean "roster
date".

### 2. One additive migration

```sql
ALTER TABLE early_vote_day ADD COLUMN vote_period TEXT NOT NULL DEFAULT 'early';
```

Values are `early` and `election_day`. Existing rows take the default, so no backfill runs. The
`CREATE TABLE` gets the same column, and a `day_migration_statements(columns)` helper applies the
`ALTER` from `_ensure_schema` only when the column is absent, the pattern `geo_migration_statements`
already uses.

The column records which portal report produced the file. Comparing against the election date at
read time was rejected: `early_vote_election_calendar` is empty in `ev-53815`, and a Worker constant
would need editing for every election.

`_DAY_UPSERT_SQL` writes the value on insert and on conflict. `EarlyVoteStore.write_database` and
its implementers take it as a required argument with no default, so no code path can reset
`election_day` to `early`. `_DAY_LIST_SQL`, `_DAY_LOOKUP_SQL` (the `?ev_date=` path) and the roster
`_DAYS_SQL` read it, and `EvDayRow` and `RosterDay` carry it.

### 3. Fetch

- A new `pull_election_day_roster_file` requests `EVR_STATEWIDE_ELECTIONDAY` and decodes the
  envelope. The ZIP handling (`_check_zip_limits`, reading `STATEWIDE_VOTER_INFO.csv`) moves out of
  `parse_ed_statewide_voter_records` into one helper in `civix/evr.py` that both use, so there is no
  second copy.
- The backfill plan adds the election date to the early-voting dates once it is not in the future.
  The existing future-date skip covers the time before the election.
- `run_backfill` picks the pull by date, passes `prepared=` to `sync_early_vote`, and sets
  `vote_period`.
- A missing or empty report counts as `skipped_empty`, not a failed day, so a run before the portal
  publishes stays green.

### 4. Normalize the header when canonicalizing

`canonicalize_roster_csv` renames `COUNTY_NAME` to `COUNTY` through a named constant, before hashing.
Early-vote files already use `COUNTY`, so their bytes and hashes do not change.

Adding `COUNTY_NAME` as a loader alias was rejected. `stream_all_days` writes one header for all days
and assumes every file matches it, and the row hash covers the header text. A uniform stored header
keeps the all-days CSV correct and keeps re-runs diffing cleanly. `MAIL-IN` rows in the Election Day
file stay as the portal reports them.

### 5. Calculation and roster routes

| Route | Result after this change |
|---|---|
| `/elections`, `/elections/{id}` | Day 18 is listed, with `vote_period`. `last_ev_date` is 2026-11-03 and `total_rows` includes it |
| `/elections/{id}/roster` | Election Day rows follow, with `EV_DATE` 2026-11-03 under the same header |
| `/elections/{id}/roster/{ev_date}` | Works for 2026-11-03 |
| `/elections/{id}/roster/{ev_date}/geo` | Works for 2026-11-03 once `VOTER_GEO_PAGES` is raised (section 6) |
| `/aggregates/totals`, `county`, `by-method` | Cumulative bodies include Election Day. `?ev_date=2026-11-03` returns that day. `through_ev_date` is 2026-11-03 |
| `/aggregates/by-day`, `county-by-day` | An 18th entry with cumulative totals, and `vote_period` on every entry |
| Deduplicated fields (ADR-016) | `unique_voters`, `new_voters` and `restated_rows` include Election Day after `first_seen_build` |

`vote_period` is the only new response field. `_rollup_days` and the one-day path copy it from the day
row. `BODY_SHAPE` moves from `b2` to `b3`, so bodies cached in the old shape are not served.

Consumers should note one change in meaning: cumulative `in_person` and `total` now cover early
voting plus Election Day. The early-only figures remain available by summing `by-day` entries where
`vote_period` is `early`.

The portal has revised the early-vote files since the last ingest: on 2026-11-09 the 10-18 file had
781 rows against 767 in D1, and 10-17 had 3,779 against 3,675. All 17 early days hash differently
from D1, so the load updates them too. Final totals are therefore the sum of the loaded files, not
fixed in this document. At the numbers D1 held before the revisions, they would be 9,715,257 rows.
The overlap between the Election Day file and the early rosters is not known until the build runs.

### 6. Keep each D1 query inside its limits

D1 allows 30 seconds per query and, since 2026-11-04, 50 MB per result. Each constant below is
sized so that no query reads more rows than one does today.

| Constant | Today | Proposed | Largest range for Election Day |
|---|---|---|---|
| `first_seen.PARTITIONS` | Last VUID digit (11 queries) | Last two digits (101 queries) | See below |
| `VOTER_GEO_KEY_RANGES` | 16 | 48 | About 43,000 rows (today's largest is about 54,000) |
| `early_vote_roster_rows.KEY_RANGES` | 16 | 48 | About 43,000 rows |
| `ALIAS_KEY_RANGES` | 4 | 16 | About 130,000 rows (today's largest is about 215,000) |

`first_seen_build` is the tightest. On 2026-11-09 a one-digit partition took 16.96 s over the 7.6M
rows; 9.7M rows would put it near 22 s. A two-digit partition took 1.35 s in the same session, though
later queries may have benefited from a warm cache. The time is in the `GROUP BY`, not the scan, so
finer partitions do help.

A voter's rows still all land in one partition, so the sum is unchanged. The cost is about nine
times more rows read per build, and the build only runs when the roster signature changes. D1
returned "overloaded" (7429) to a query run alongside these measurements. The pause between
partitions and the rate-limit retry from #141 stay in place for that.

`VOTER_GEO_PAGES` also has to rise, for a different reason. Each `/geo` page is one D1 text cell, D1
caps a string at 2 MB, and 256 pages were sized for a 1,000,000-row day. At the code's estimate of
340 bytes per voter, Election Day would need about 2.75 MB per page, so the route would fail. At 640
pages it is about 3,200 voters and 1.1 MB. `LARGEST_DAY_ROWS` in
`tests/unit/test_voter_geo_page_size.py` moves to 2,100,000. This changes the page count for every
day: clients that read `pages` from the response are fine, and any that hardcoded 256 must change.

## Rollout

Each step needs the owner's go-ahead, because it changes the production database or Worker.

1. Record a D1 Time Travel bookmark for `ev-53815`.
2. Run `python -m texas_turnout_scraper.early_vote_roster_rows_backfill --election-id 53815 --apply`
   from the new code, in the 1Password Environment `local_ev_backfill.sh` uses. It calls
   `_ensure_schema`, which applies the `ALTER`, and has nothing else to do because all 17 days are
   already loaded. Confirm 17 rows read `early`.
3. Deploy the Worker. **It must follow step 2**: `load_day_rows` turns any query error into an empty
   list, so a Worker reading a missing column would serve no days rather than fail visibly.
4. Run `scripts/local_ev_backfill.sh dry_run` and confirm 2026-11-03 appears with the expected row
   count and no error. A dry run has no stored hashes, so it lists all 18 days and cannot show which
   are new.
5. Run `scripts/local_ev_backfill.sh`. It also applies the portal's early-vote revisions. It covers every step, including `first_seen_build`, `geo_load`
   and `voter_geo_build`. Deduplicated counts read null with `dedupe.status = stale` until
   `first_seen_build` finishes, which is the behavior ADR-016 chose.
6. Run the smoke checks below.
7. Re-run step 5 after the portal certifies the election. An unchanged file writes nothing; a changed
   one applies only the row differences.

The cron stays stopped, because the remaining work is one load and one re-load.

**Rollback:** restore the bookmark. The column can stay; it is harmless with every row `early`.

## Acceptance

Automated, before any D1 write:

- `uv run pytest tests/unit` and `tests/verify`, `ruff`, and `uv run ty check`.
- New unit tests: the header rename leaves early-vote bytes and hashes unchanged; a fixture ZIP goes
  through the pull; the plan includes the election date only once it has passed; a missing report is
  `skipped_empty`; the migration is idempotent and a no-op on new databases; `by-day` carries
  `vote_period`.
- Partition coverage: every VUID falls in exactly one partition.
- The vendored Worker copy under `infra/preview/worker/python_modules` matches `src`.

After the load, in `ev-53815`:

- `early_vote_roster_version` has 18 days; 2026-11-03 has `row_count` 2,073,832 (or the then-current
  file's count) and `reject_count` 0.
- Live rows for `day` 739923 equal `row_count`.
- Every `first_seen` partition took under 20 s, and `dedupe.status` is `current`.
- `voter_geo_meta` `live_rows` equals the sum of `row_count` over all 18 days, and `no_alias_vuids` and `unknown_county_vuids` are 0.
  The increase in `unmatched_alias_vuids` is reported.

Against the deployed API:

- `/aggregates/by-day` has 18 entries, and the last has `vote_period = election_day`.
- `/aggregates/totals` `total` equals the sum of `row_count` over all 18 days.
- `/roster/2026-11-03/geo` returns every page without error, and the page voter counts sum to the
  day's distinct listings.
- `/roster/2026-11-03` and the all-days roster download complete, with one header and no worker
  error. A 116 MB single day is 2.4 times the largest served so far, so this has to be observed.

The 0.26% gap to the portal's county table and the Election Day and early-roster overlap are
reported, not gating.

## Files

In scope: `early_vote_sync.py`, `early_vote_backfill.py`, `early_vote_roster.py`,
`early_vote_roster_rows.py`, `early_vote_d1_read.py`, `early_vote_d1_roster.py`,
`early_vote_d1_precompute_read.py`, `early_vote_aggregates.py`, `first_seen.py`,
`first_seen_build.py`, `geo_load.py`, `voter_geo_build.py`, `voter_geo_tables.py`, `civix/evr.py`,
`infra/preview/worker/src/ev_api/read.py`, `roster_routes.py`, the vendored copies, tests, and
`docs/CIVIX_EVR_API.md`.

Not touched: `early_vote_roster_row` and the other table definitions apart from `early_vote_day`,
`infra/ev_ingest` (schedule), GitHub Pages `data/`, the MCP server and CLI.

`voter_geo_build.py`, its vendored copy and `scripts/ev_ingest_steps.sh` had uncommitted edits when
this was written. Those should be committed or resolved before implementation starts.

## Out of scope

- `STATEWIDE_POLLING_PLACE_INFO.csv` and the per-county Election Day ZIP endpoint.
- `repeat_build`, still paused. When it resumes, an Election Day listing of someone on an early
  roster counts as a repeat; whether that is correct should be decided then.
- `voter_link` and `DATED_MODES`. `GEN26` is not in `DATED_KEYS` and `early_vote_voter` is empty, so
  the early-voting mode letters do not apply to Election Day rows yet.
- Elections other than 53815. The code is generic, but only this election has a database.

## Consequences

- The database grows from 4.90 GB to an estimated 6.2 GB by row proportion, of a 10 GB cap that
  cannot be raised.
- A single-day read of Election Day is the largest response the Worker has served.
- A row in the early-vote tables can now be a non-early vote, which `vote_period` marks at the day
  level.

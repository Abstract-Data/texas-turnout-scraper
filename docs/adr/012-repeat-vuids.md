# ADR-012: Repeat VUIDs and excess counts in D1

**Date:** 2026-10-24
**Status:** Accepted

## Context

On 2026-10-24 the election's roster held 3,712,481 live rows for 3,054,701 distinct VUIDs. Every
repeated VUID appeared exactly twice. 656,734 voters appeared on two different days, and in each
case the second listing was early day 11 (2026-10-24), whose file re-lists voters from earlier days,
mostly day 6. 1,046 voters repeated within one day. 619,602 repeated voters were IN-PERSON, 38,176
MAIL-IN, and 2 had both methods. The portal serves exactly the file we stored, so this is not a
loader bug, and we do not guess why the portal lists them again. The API's `totals` sums rows, so
it overstates distinct voters by about 21%.

The owner decided that the headline total becomes the distinct voter count. A later Worker change
moves the API to it. This change builds the data. Raw counts stay: nothing is deleted or hidden.

## Decision

1. **First appearance.** A repeated voter is attributed to their first appearance: the earliest
   `ev_date`, then within one day the lowest `(row_hash, occ)` key. Only live rows count
   (`removed_in IS NULL`).
2. **`early_vote_vuid_repeat`** (`vuid` key, `WITHOUT ROWID`) has one row per VUID that is counted
   more than once or was removed at least once. A VUID seen once and never removed has no row.
   Columns: `n_counted` (live rows), `n_removed` (removal events in `early_vote_day_change`),
   `first_day` (NULL when no live row is left), `counted_days` (distinct ISO dates ascending),
   `appearances` (one `{"day", "method", "county"}` per live row in first-to-last order, so a
   same-day repeat appears twice), `removed_days` (distinct ISO dates ascending of the
   `change_kind = 'removed'` events), `flags` (1 same-day repeat, 2 cross-day repeat,
   4 cross-method repeat, 8 removed at least once) and `src_hash`.
3. **`early_vote_repeat_excess`** (`ev_date, county, voting_method` key) holds `n`, the number of
   non-first appearances at those coordinates. The distinct count of any slice is its raw row count
   minus its excess. Over all slices, raw rows minus total excess equals
   `COUNT(DISTINCT id_voter)`. A test checks both against an SQL window-function oracle on a random
   synthetic roster.
4. **`early_vote_repeat_meta`** (`name`, `value`) holds `signature`, `raw_rows`,
   `distinct_voters`, `repeat_voters` (`n_counted > 1`), `excess_rows` and `removed_voters`. It
   holds no build time, so identical inputs give identical rows. It is written last.
5. **Signature.** sha256 over `REPEAT_VERSION`, the sorted `(ev_date, version, sha256, row_count,
   reject_count)` rows of `early_vote_roster_version`, and the `COUNT(*)` and `MAX(recorded_at)` of
   the election's `early_vote_day_change` rows. When an apply finds the stored signature equal, it
   stops there and sends no statement besides the schema step every loader runs.
   `REPEAT_VERSION` was added to the owner's signature definition so that a change to how rows
   are derived rebuilds them on the next run, not only when the roster next changes.
6. **One pass.** The build reads live rows one day (ascending) and one sixteenth of the row-hash
   space (ascending, `ORDER BY row_hash, occ`) per query, so rows arrive in first-appearance order.
   It keeps a `first` map of VUID to an interned `(day, county, method)` tuple. Only when a VUID is
   seen again does it keep a list of that voter's appearances, and each later appearance adds one to
   that coordinate's excess. Removal events are read one change-log day at a time (a primary-key
   seek for the next day) in keyset pages of 10,000. Stored hashes are read in keyset pages of
   10,000 VUIDs, as text, because D1's JSON results cannot carry every signed 64-bit integer
   exactly. Only new or changed rows are upserted, in `json_each` batches of at most 2,000 rows and
   900 KB. Rows that no longer qualify are deleted in the same bounded batches. The excess table is
   replaced by diff. The meta rows come last.
7. **Models.** `VuidRepeat` (no table) validates each row through `model_validate`, and
   `VuidRepeatRow` is the table, the ADR-011 split. `RepeatExcess` validates each changed excess
   row. The DDL and SQL live in `repeat_tables.py`, which imports no sqlmodel or sqlalchemy, and a
   drift test pins them to the models. `early_vote_sync` creates the tables with the others. Column
   types are `TEXT`, `INTEGER` and `JSON` (`sa_type`), as the owner specified, not the `VARCHAR`
   that a plain `str` field generates. SQLite gives both TEXT affinity.
8. **CI.** The scheduled sync runs `python -m texas_turnout_scraper.repeat_build --apply` after the
   geography step. A dry run reads, compares when the tables exist, and reports. It never runs the
   schema step and never writes. Logs, output and validation errors carry counts and field names
   only.

## Consequences

- On synthetic data (150,000 rows over 11 days, 20% of voters listed twice), the build peaked at
  161 bytes per roster row under tracemalloc and took about 3.7 s, including the sqlite fake's
  reads. On the real roster this predicts roughly 0.6 GB of peak memory. That is a guess from
  bytes per row, not a measurement on D1.
- A changed day costs one full read of the live rows: 16 queries per day, about 21,000 rows each.
  Unchanged runs read the version rows and two aggregates of the change log, and nothing else.
- `MAX(recorded_at)` has no index, so the signature check scans the election's change-log rows.
  The change log is far smaller than the roster.
- A roster row removed without a change-log event (a reload whose diff is not logged) drops out
  of `appearances` but sets no flag 8. Flag 8 means "the change log recorded a removal", nothing
  more.
- Readers that need one voter's day keep reading the roster: a VUID with no row here has exactly
  one live row.

# Early-vote VUID anomaly tracking (D1)

## Purpose

Civix publishes one statewide early-vote roster per calendar day, and each file holds
only that day's voters (consecutive days share no VUIDs). When a new file replaces an
older one for the same `ev_date`, we want durable, queryable
records of VUID-level anomalies: duplicate rows inside one file, voters who
disappear on a later publish, and county or voting-method changes for the same
VUID.

This design supports **read-only analysis and future persistence** — not live
election-night serving.

## Relation to `early_vote_day_change`

The loader already writes `early_vote_day_change` (see `early_vote_day_changes.py` and the
RUNBOOK). It records only the VUIDs **added** and **removed** when a day's file changes, and it
runs on every changed load. This design is the broader, still unwired analysis: duplicates inside
one file and `method_changed` / `county_changed` for a VUID present in both versions. Those
cases are not in `early_vote_day_change`. If this is ever wired in, read the prior version from
the R2 snapshots (`elections/<id>/<ev_date>/<sha256>.csv.gz`) rather than from D1, because D1
keeps only the current file for a day, and treat `dropped_from_later` as the same fact as
`removed` instead of storing it twice.

## Not applied live

- DDL in `early_vote_vuid_dup.py` is **not** executed by `early_vote_sync`,
  backfill, Terraform apply, or the preview Worker in this change set.
- No workflow, D1 writer, or KV path is modified.
- Applying the schema to preview/production D1 is a **separate** follow-up (with
  its own review and explicit apply approval).

## Not wired to sync

A future job will:

1. Decode roster CSV (same canonicalization as `early_vote_roster.canonicalize_roster_csv`).
2. Load the prior and current `file_id` for one `ev_date` from `early_vote_day`.
3. Call `compare_early_vote_rosters` and bulk-insert into `early_vote_vuid_dup`.

That wiring is intentionally out of scope here so schema and detection logic can
land and be unit-tested without touching the publish path.

## DDL summary

Module: `src/texas_turnout_scraper/early_vote_vuid_dup.py`

| Object | Role |
|--------|------|
| `early_vote_vuid_dup` | One row per `(election, ev_date, file_id, id_voter, anomaly_kind)` |
| `idx_early_vote_vuid_dup_election_voter` | Lookup by election + VUID |
| `idx_early_vote_vuid_dup_election_date_kind` | Rollups by day and kind |
| `idx_early_vote_vuid_dup_election_file` | Per-file audits |
| `early_vote_vuid_dup_day` | Per-day aggregate counts (view) |

### Table `early_vote_vuid_dup`

| Column | Notes |
|--------|--------|
| `source_election_id` | Civix election id (string) |
| `ev_date` | ISO `YYYY-MM-DD` |
| `file_id` | File where the anomaly is recorded (usually the **current** publish) |
| `prior_file_id` | Earlier file for cross-file kinds; `NULL` for `within_file_dup` |
| `id_voter` | Texas VUID (string, leading zeros preserved) |
| `anomaly_kind` | See below; enforced by `CHECK` |
| `county`, `voting_method` | Current-side values when applicable |
| `prior_county`, `prior_voting_method` | Prior-side values when applicable |
| `occurrence_count` | Row count for this VUID in the file (`within_file_dup`); else `1` |

Primary key: `(source_election_id, ev_date, file_id, id_voter, anomaly_kind)`.

### View `early_vote_vuid_dup_day`

Grouped by `(source_election_id, ev_date, file_id)`:

| Column | Meaning |
|--------|---------|
| `dup_vuids` | Distinct VUIDs with `within_file_dup` |
| `dup_excess_rows` | Sum of `occurrence_count - 1` for `within_file_dup` |
| `dup_multi_method` | `within_file_dup` rows where duplicate lines disagree on method |
| `dup_multi_county` | `within_file_dup` rows where duplicate lines disagree on county |
| `dropped_vuids` | Distinct VUIDs with `dropped_from_later` |
| `method_changed_vuids` | Distinct VUIDs with `method_changed` |
| `county_changed_vuids` | Distinct VUIDs with `county_changed` |

## `anomaly_kind` semantics

| Kind | When emitted |
|------|----------------|
| `within_file_dup` | Same `id_voter` appears more than once in **current** file rows. `prior_county` / `prior_voting_method` are set only when duplicate lines disagree on county or method. |
| `dropped_from_later` | VUID present in **prior** file but absent from **current** (later publish for the same `ev_date`). |
| `method_changed` | VUID in both files; the set of `voting_method` values differs between prior and current. |
| `county_changed` | VUID in both files; the set of `county` values differs between prior and current. |

Both `method_changed` and `county_changed` may be emitted for one VUID.

## Pure compare API

```python
from texas_turnout_scraper.early_vote_vuid_dup import (
    EarlyVoteRosterRow,
    EarlyVoteRosterSnapshot,
    compare_early_vote_rosters,
)
```

`EarlyVoteRosterRow` carries `county`, `id_voter`, and `voting_method` only (no
names). Snapshots add `source_election_id`, `ev_date`, and `file_id` metadata.

## PII

Never log or commit real `id_voter` values. Tests use synthetic VUIDs only.

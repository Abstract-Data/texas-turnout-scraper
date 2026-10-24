# ADR-011: Voter attributes, history and scores in D1

**Date:** 2026-10-24
**Status:** Accepted

## Context

The roster tells us who has voted early in the current election, not who those voters are. The
owner wants, for each of them, a few voterfile attributes, their vote history, scores derived from
it, which early day they voted on in the recent elections the portal still publishes, and their
age. The voterfile (3.68 GB, 18.9M rows, 47 columns) is PII and stays on the owner's machine.
Nothing in the Worker reads these tables yet; a later change joins them to roster rows by
`id_voter = vuid`.

## Decision

1. **Only voters who turned out, only the chosen columns.** The target set is the distinct VUIDs
   of live rows (`removed_in IS NULL`) in the election's `early_vote_roster_row`, read one day and
   one sixteenth of the row-hash space per query. The DuckDB query selects exactly `VUID`, `SEX`,
   `DOB`, `EDR` and the 17 history columns (`GEN24` to `GEN18`, `PRI26` to `PRI18`, `RO26` to
   `RO18`) for those VUIDs and nothing else. County, precinct, district and name or address
   columns are never read. Voterfile rows outside the target set are never stored. There is no
   join table: the roster's `id_voter` is the key.
2. **Local only.** `python -m texas_turnout_scraper.voter_link` runs on the owner's machine with
   `--apply` or `--dry-run`. No workflow runs it. The target VUIDs go to DuckDB through a CSV in a
   temporary directory that is removed after the read (binding a 3M-element list measured over
   100 s; the file took 0.64 s).
3. **Tables.** `early_vote_voter` (`vuid` key, `WITHOUT ROWID`), `early_vote_election_calendar`,
   `age_tier`, `early_vote_voter_reject` and `early_vote_voter_meta`, as SQLModel models in the
   loader-only `voter_models.py`. The DDL, seeds and SQL are committed in `voter_tables.py` (no
   sqlmodel, sqlalchemy or duckdb) and pinned by a drift test, the ADR-009 pattern.
   `early_vote_sync` creates them with the other tables. History codes are stored as in the file
   (stripped), NULL when blank. VUIDs and `source_election_id` stay text; VUIDs are zero-padded to
   10 digits.
4. **Validation.** `parse_voter` builds the row and calls `Voter.model_validate`. `Voter` is a
   non-table SQLModel; `VoterRow(Voter, table=True)` is the table the DDL comes from. A table
   model sends every field set through SQLAlchemy's attribute instrumentation, which measured
   24.1 s per 200,000 voters against 8.1 s for the split (three runs each). A history code
   outside its column's set (`V VE VA` for generals, `D R DE RE DA RA` for primaries and runoffs)
   or a sex outside `F M U` (blank included) rejects the voter: the row goes to
   `early_vote_voter_reject` with the 21 selected cells and a reason of field names and error
   types. A bad `DOB` or `EDR` does not reject: it is stored as NULL and sets `flags` bit 1 (DOB)
   or 2 (EDR). A date is good when it is 8 digits, a real calendar date, not after today in
   America/Chicago, and in or after 1880 (DOB) or 1900 (EDR). A blank date is NULL with no flag. Sex is
   rejected, not coerced, so bit 4 is unused.
5. **Scores** (`voter_scores.derive`, pure, `SCORE_VERSION = 1`). The suffix of a code gives the
   mode (`E` early in person, `A` absentee, none Election Day); the first letter of a primary or
   runoff code is the party. Counts are over the 17 voterfile columns. `primary_pattern` uses
   primaries and runoffs together; `general_pattern` uses presidential generals (`GEN24`,
   `GEN20`) and midterm generals (`GEN22`, `GEN18`); off-year generals are counted in `n_gen_off`
   and decide no label. `typical_mode` is the most common mode, `mixed` when the top two tie, NULL
   when the voter never voted. `early_or_absentee_share` and `typical_earliness` are rounded to 4
   decimals.
6. **Dated history.** The portal lists early days only from 2025: the Nov 2025 amendment election
   (51031, 12 days), the 2026 primaries (53813 R, 53814 D, 11 days) and runoffs (58314 D,
   58315 R, 5 days). `date_turnout_id` k is "Early Day k". With `--dated-history` the link fetches
   each day's statewide file (`CivixClient.fetch_statewide`, paced), caches it as
   `<civix id>/<date>.<sha256>.bin` so a rerun fetches nothing, and keeps each target voter's
   lowest k and mode (`IN-PERSON` is `E`, `MAIL-IN` is `A`). Both parties of a primary fill one
   column. The voterfile has no `GEN25`, so `gen25_day` and `gen25_mode` come only from the portal.
   Earliness for day k of N is `(N - k) / (N - 1)`; `typical_earliness` averages it over the
   dated elections the voter appears in. GEN25 counts toward earliness but not toward `n_voted`.
   Earlier elections are mode-only. The calendar is a committed constant, checked against
   `list_elections()` on every dated run; a mismatch is logged with dates and counts.
7. **Age.** `age_tier` holds seven tiers (18-24, 25-34, 35-54, 55-64, 65-74, 75-84, 85+ with
   `max_age` 200). The view `early_vote_voter_age` gives whole years as of `date('now')` and as of
   the calendar row with `is_current = 1` (GEN26, 2026-11-03), and the tier by a join on
   `age BETWEEN min_age AND max_age`. A Feb 29 birthday has passed on Mar 1. NULL `dob` or an age
   under 18 gives a NULL tier.
8. **Writes.** `src_hash` is a signed 64-bit BLAKE2b of the score version, the 21 cleaned cells,
   the parsed dates and the dated entries. The link reads stored hashes as text (`CAST(src_hash AS
   TEXT)`: D1's JSON results cannot carry every 64-bit integer exactly), by slices of at most
   20,000 target VUIDs, and upserts only new or changed voters in `json_each` batches of at most
   2,000 rows and 900 KB, `ON CONFLICT(vuid) DO UPDATE`. A reject whose voter is now valid is
   deleted from the reject table. Voters are never deleted. The meta rows (voterfile name, size
   and sha256, election id, score version, a hash of the seeds, counts) are written last; when
   nothing changed and the meta matches, an apply sends no statement at all. A dry run reads, and
   compares when the tables exist, but never runs the schema step and never writes.
9. **No PII in logs.** Logs, output, reasons and exceptions carry counts, field names and error
   types. DuckDB errors are re-raised with their type name only, since their messages can quote a
   line of the file.

## Consequences

- About 8 s per 200,000 voters for validation, scores, hashing and batch building, so about
  2 minutes for 3M voters on the owner's machine, plus the voterfile scan and the sha256 of the
  file.
- `date('now')` is the UTC date, so `age_today` turns over at 7 pm Central (6 pm in summer).
- `CREATE VIEW IF NOT EXISTS` does not replace an existing view. Changing the view needs a
  `DROP VIEW early_vote_voter_age` first.
- The upsert carries no `WHERE` on its `DO UPDATE`: only changed rows are sent, so none is needed,
  and the statement shape is the one the roster and geography loaders already run on D1.
- A voter dropped from the roster keeps their row. A later reader joins on live roster rows.
- The table DDL types are what SQLModel generates (`VARCHAR`, `INTEGER`, `FLOAT`), which SQLite
  treats as TEXT, INTEGER and REAL affinity.
- The calendar and tiers are seeded by the link, not by the sync, so the view's election-day age is
  NULL until the first apply. No voter rows exist before then either.

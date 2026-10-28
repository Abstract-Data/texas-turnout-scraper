# ADR-015: Per-voter geography in D1

**Date:** 2026-10-27
**Status:** Proposed (Stage 1 built; Stage 2 waits for the owner's explicit approval)

## Context

Each early-vote roster voter should carry a county, a precinct, and a congressional, state senate
and state house district. Where the owner approves it, the voter should also carry a residential
point with its source and quality. ADR-010 already loads the state precincts, the plans, and an
alias from each roster `(county, precinct)` pair to a state precinct. It serves them as one
lookup document. A consumer could join that document to the roster. The owner wants the join
done once, in batch, per VUID, so that a consumer joins on VUID and a later point can sit next to
it.

The roster routes stream stored bytes and stay byte-exact (ADR-010 §13). Nothing here changes
them. Geography is computed after the roster load and stored. It is never computed at read time.

## Decision

1. **Two stages, two tables, one writer each.** Stage 1 (`voter_geo`) is derived from the roster
   and the ADR-010 tables, and adds no new PII. Stage 2 (`voter_geo_point`, not built yet) holds
   residential points from the owner's voterfile and is written only on the owner's machine. The
   two never write the same table, so a Stage 1 rebuild cannot overwrite a Stage 2 point and the
   restricted table can be dropped or scoped on its own. The read route joins them.
2. **Stage 1 rows.** One row per VUID with a live roster row (`removed_in IS NULL`), keyed by the
   roster's `id_voter` through `voterfile.normalize_vuid`. `county_fips` comes from the roster
   county name by its letters only (`geo_load.county_key`). `pctkey` is the stored
   `geo_precinct_alias` for the exact roster pair, NULL when the alias is `unmatched` or missing.
   `cd`, `sd` and `hd` come from `geo_precinct`. `split_flag` packs `split_cd`, `split_sd` and
   `split_hd` as bits 1, 2 and 4, NULL when there is no `pctkey`. Voterfile district columns are
   not used, as in ADR-010.
3. **Which listing wins.** A VUID listed more than once takes its first appearance, the ADR-012
   rule (earliest day, then the lowest `(row_hash, occ)`). `flags` bit 1 means the live rows name
   more than one `(county, precinct)`, and bit 2 means more than one county. Those voters are
   counted in the build output and never guessed between.
4. **Build.** `python -m texas_turnout_scraper.voter_geo_build --election-id <id> --dry-run |
   --apply`. It follows the ADR-012 build. A signature over `VOTER_GEO_VERSION`, the roster
   version rows, the `geo_meta` sha256 and a hash of the alias rows skips an unchanged run. One
   pass reads live rows one day and one row-hash sixteenth per query. Only rows whose `src_hash`
   changed are upserted, in `json_each` batches of at most 2,000 rows and 900 KB. VUIDs no longer
   live are deleted. The meta rows are written last. `matched_at` is set only on rows a run
   writes. A dry run reads and compares, never runs the schema step, and never writes. Output and
   logs carry counts only.
5. **Not in the scheduled sync yet.** The first build writes one row per distinct live VUID, about
   3.68M rows for 53815. The ADR-009 addendum measured 3.7M row writes through the REST API at 33
   minutes, far past the 5-minute workflow cap (ADR-014). The step is therefore not added to
   `early-vote-sync.yml`. The first apply runs once from the owner's machine with the owner's
   approval. After that a changed day writes only its new or changed VUIDs. The build is shaped to
   become one ADR-014 Workflow step after the roster ranges land (its reads are per day and per
   row-hash range already).
6. **Tables and drift.** `voter_geo` (`vuid` key, `WITHOUT ROWID`) and `voter_geo_meta`
   (`name`, `value`). SQLModel models in the loader-only `voter_geo_models.py` (the ADR-011 split:
   a non-table model validates, the table model generates the DDL). The DDL and SQL are committed
   in `voter_geo_tables.py`, which imports no sqlmodel, sqlalchemy or duckdb, and a drift test
   pins them. `early_vote_sync` creates them with the other tables.
7. **Read route.** `GET /elections/{id}/roster/{ev_date}/geo?page=k`, `k` in `0..63`, behind the
   roster gate: only an active roster-scoped consumer token. The Access service credential gets
   401 and an aggregates token gets 403, as on the roster routes. Page `k` is the `k`th of 64
   row-hash ranges of that day's live rows. The pages partition the day's rows, so every live VUID
   of the day is on at least one page and is distinct within a page. A VUID listed twice on one
   day under two row hashes (ADR-012's same-day repeat, 1,046 voters on 2026-10-24) can appear on
   two pages with the same values. A consumer dedupes on VUID. Partitioning by VUID instead would
   need an index on `id_voter`. One D1 query
   returns the page as one row with a count and a JSON text cell (`json_group_array`, the ADR-010
   §12 trick), and the
   Worker wraps it without parsing, so Worker CPU stays constant per page. That is why this join
   is affordable per request when parsing a stored roster day is not: it reads about 6,000 indexed
   rows and never touches the stored file. Each voter carries `vuid`, `county_fips`, `pctkey`,
   `cd`, `sd`, `hd`, `district_source`, `split_flag`, `flags`, `lat`, `lon`, `geocode_source`,
   `geocode_quality`, `address_points_vintage` and `matched_at`, and never a name, voting method
   or county text. The response is
   `Cache-Control: private, no-cache` with an ETag from the day's version, the build signature and
   the page. Missing tables give 503 `voter_geo_not_loaded`.
8. **Point invariant.** `lat` and `lon` are not null if and only if `geocode_quality` is
   `address_point`. Stage 1 never emits a point: `geocode_quality` is `stated_precinct` with a
   `pctkey`, `not_located` without one, and NULL when the VUID has no row yet. A precinct interior
   point is never stored or served as a residence. Stage 2 puts the invariant in its table as a
   `CHECK`, since `CREATE TABLE IF NOT EXISTS` cannot add one later.

## Stage 2 (proposed, waits for approval)

Stage 2 runs only on the owner's machine, like ADR-011. No workflow runs it. It reuses the
voterfile-audit-pipeline at the pinned rev through an optional extra that only this module
imports. The pipeline needs Python 3.11 or later, so the extra carries that marker.

- Read the target VUIDs from D1 as `voter_link.target_vuids` does and pass them to DuckDB through
  a temporary CSV.
- Normalize the selected rows with the pipeline's ingest. This voterfile vintage has no `RSTPRE`
  column, and the directional survives only in `RADDRESS`. The pipeline's ingest recovers it
  (`_recover_pre_directional`, pipeline ADR-0023) and renames the components to the names
  `geocode_frame_offline` expects by default.
- Geocode with `geocode_frame_offline` against the TxGIO address-point parquet (vintage
  2026-03-18). Matching is exact on a normalized key and never fuzzy. `geocoding.geocode_batch`
  is never used, because it sends addresses to the Census geocoder and Nominatim.
- Assign districts with `assign_districts` on the matched points, against the plan layers.
- Write `voter_geo_point (vuid, lat, lon, cd, sd, hd, geocode_source, geocode_quality,
  address_points_vintage, matched_at)` with the `CHECK` of §8, through the same diff-only batches.

## Consequences

- Stage 1 carries no information the ADR-010 lookup document does not already hold. It turns a
  per-pair join into a per-VUID row so that a consumer joins on VUID, and so that Stage 2 has a
  row to sit beside.
- Size. About 3.68M rows. Each row holds a 10-character VUID, a few short codes, two integers and
  a 20-character timestamp. That is a guess of under 0.5 GB with the key, well under D1's 10 GB
  per database. It is not yet measured.
- A voter dropped from the roster loses their row on the next build.
- A geography change (a new `tx_geo.json`) changes the signature, so the next build rewrites the
  rows whose districts moved.
- The route serves VUIDs with precincts. It is roster scope for that reason, even though it
  carries no names.

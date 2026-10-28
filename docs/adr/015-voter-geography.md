# ADR-015: Per-voter geography in D1

**Date:** 2026-10-27
**Status:** Proposed (both stages built; neither applied to the production D1)

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
   and the ADR-010 tables, and adds no new PII. Stage 2 (`voter_geo_point`) holds
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
7. **Read route.** `GET /elections/{id}/roster/{ev_date}/geo?page=k`, `k` in `0..255`, behind the
   roster gate: only an active roster-scoped consumer token. The Access service credential gets
   401 and an aggregates token gets 403, as on the roster routes. Page `k` is the `k`th of 256
   row-hash ranges of that day's live rows. The pages partition the day's rows, so every live VUID
   of the day is on at least one page and is distinct within a page. A VUID listed twice on one
   day under two row hashes (ADR-012's same-day repeat, 1,046 voters on 2026-10-24) can appear on
   two pages with the same values. A consumer dedupes on VUID. Partitioning by VUID instead would
   need an index on `id_voter`. One D1 query
   returns the page as one row with a count and a JSON text cell (`json_group_array`, the ADR-010
   §12 trick), and the
   Worker wraps it without parsing, so Worker CPU stays constant per page. That is why this join
   is affordable per request when parsing a stored roster day is not: it reads about 4,000 indexed
   rows and never touches the stored file. Each voter carries `vuid`, `county_fips`, `pctkey`,
   `cd`, `sd`, `hd`, `district_source`, `split_flag`, `flags`, `lat`, `lon`, `geocode_source`,
   `geocode_quality`, `address_points_vintage` and `matched_at`, and never a name, voting method
   or county text. The response is
   `Cache-Control: private, no-cache` with an ETag from the day's version, the build signature and
   the page. Missing tables give 503 `voter_geo_not_loaded`. The JSON cell is one D1 string, and
   D1 caps a string at 2 MB. A voter with a point takes about 340 bytes, so 256 pages keep a
   1,000,000-row day (2026-10-24 re-listed 656,734 earlier voters on top of its own) under
   1.5 MB per page. `tests/unit/test_voter_geo_page_size.py` pins it.
8. **Point invariant.** `lat` and `lon` are not null if and only if `geocode_quality` is
   `address_point`. Stage 1 never emits a point: `geocode_quality` is `stated_precinct` with a
   `pctkey`, `not_located` without one, and NULL when the VUID has no row yet. A precinct interior
   point is never stored or served as a residence. Stage 2 puts the invariant in its table as a
   `CHECK`, since `CREATE TABLE IF NOT EXISTS` cannot add one later.

## Stage 2: residential points

The owner approved Stage 2 on 2026-10-27. Writing points to the production D1 needs a separate
approval. Stage 2 runs only on the owner's machine, like ADR-011. No workflow runs it.

9. **Environment.** The pipeline needs Python 3.11 or later and this project's venv is 3.10. Stage
   2 runs in an ephemeral environment built from the pinned submodule:
   `PYTHONPATH=src uv run --no-project --python 3.12 --with ".[loader,voterfile]" --with
   ./vendor/voterfile-audit-pipeline python -m texas_turnout_scraper.voter_geo ...`. Neither
   `pyproject.toml` nor `uv.lock` names the pipeline, so CI never needs the private repo.
   `PYTHONPATH=src` is there because uv can reuse a cached wheel of this project that predates a
   change. polars, geopandas, duckdb and the pipeline are imported only inside functions, and a
   subprocess test holds that.
10. **Inputs.** Target VUIDs come from D1 as in `voter_link.target_vuids`, and reach DuckDB through
    a temporary CSV. The read selects only `VUID COUNTY RADDRESS RHNUM RDESIG RSTNAME RSTTYPE
    RSTSFX RUNUM RUTYPE RCITY RZIP CDPLANC2333 SD2022 HD2022`. Names, birth dates, mailing
    addresses and history are never read.
11. **Normalization.** The pipeline's public `ingest` requires name columns, so it is not called.
    `voter_geo` runs the same steps in the same order: null spellings, `normalize_voter_columns`,
    strip and upper-case, `_recover_pre_directional`, `_combine_address_component_columns` and
    `_combine_address_unit_columns`. This voterfile vintage has no `RSTPRE`. The directional
    survives only in `RADDRESS`, and `_recover_pre_directional` (pipeline ADR-0023) recovers it.
    The frame then carries the column names `geocode_frame_offline` expects by default. Duplicate
    VUIDs keep the last row, as `ingest` does. These are private functions at a pinned rev, so a
    submodule bump must be checked against them.
12. **No network geocoder.** `geocode_frame_offline` matches exactly on a normalized key against the
    local TxGIO parquet (vintage 2026-03-18) and never fuzzily. The pipeline's
    `stages.geography` package imports `geocoding.geocode_batch` (Census, then Nominatim) when it
    loads. `voter_geo` therefore registers a tripwire module under that name first. The real
    module never runs, and a call raises. It refuses to start if the real module is already
    loaded. A test holds this.
13. **Districts.** `assign_districts` places each matched point in the plan layers. A layer folder
    must hold exactly one `.shp`, named for `PLANC2333`, `PLANS2168` or `PLANH2316` (the ADR-010
    plans), or the run stops before any write. A point inside no polygon keeps a NULL district.
    The precinct stays the Stage 1 alias: the pipeline's precinct layer is the 2020 VTD file,
    which ADR-010 rejected.
14. **Table.** `voter_geo_point (vuid, lat, lon, cd, sd, hd, geocode_source, geocode_quality,
    address_points_vintage, src_hash, matched_at)`, `WITHOUT ROWID`, one row per matched point
    only. Three `CHECK`s: `lat` and `lon` are both set exactly when `geocode_quality` is
    `address_point`; `geocode_quality` is `address_point`; `geocode_source` is
    `txgio_address_point`. Coordinates are rounded to 6 decimals. `voter_geo_point_meta` holds a
    `signature` over the sorted `src_hash` values and the vintage, plus the vintage, the plan
    names, the pipeline rev, the voterfile name and size, and the counts. Writes are diff-only as
    in Stage 1. A point whose voter is no longer a target or no longer geocodes is deleted. An
    apply refuses to delete every stored point when nothing geocoded.
15. **Route.** When a point row exists, the route serves its `lat`, `lon` and districts with
    `district_source` `address_point` and `geocode_quality` `address_point`. Otherwise it serves
    the Stage 1 fields. The ETag adds the point signature when one exists, so an apply of points
    alone still changes it.

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

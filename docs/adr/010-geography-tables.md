# ADR-010: Geography tables in D1

**Date:** 2026-10-24
**Status:** Accepted

## Context

To place each early-vote voter on a map we need a point for the voter's county, precinct,
congressional, state senate and state house district, and a mapping from each precinct to its
districts. The state publishes the plans and the precincts as shapefiles. The roster names a
county and a county-local precinct, so roster precincts also need a mapping to the state's
precinct keys. Nothing reads these tables yet; a later Worker change joins them to roster rows.

## Decision

1. **Offline build, committed output.** `geo_build.py` reads the layers and writes
   `data/geo/tx_geo.json`. It needs the `geo` extra (shapely, pyproj, pyshp). The loader, CI and
   the Worker only read the committed JSON, so none of them installs or imports those libraries
   (a subprocess import test holds this).
2. **Sources.** Plans `PLANC2333` (38), `PLANS2168` (31), `PLANH2316` (150); the 254 Texas
   counties (`STATEFP = 48`) of `tl_2024_us_county`; and the precinct file named by
   `tx_election/precincts/current.json` (`Precincts26P`, 9,614 precincts). The Census VTD layer is
   not used: the state precinct layer matches the roster better. The build fails on a missing
   file, a folder with more than one plan, an unexpected feature count, a repeated code, or a
   source file that disagrees with the pipeline's `.checksums.json`.
3. **Area math in EPSG:3083** (NAD83 / Texas Centric Albers Equal Area). Every layer is projected
   from the CRS in its own `.prj` with `always_xy=True`. An equal-area projection makes overlap
   shares true area ratios. The plans ship in the Texas State Mapping System (EPSG:3081, a
   Lambert conformal projection), which is not equal-area.
4. **Points.** A place's point is shapely's `representative_point()` of its (multi)polygon in
   EPSG:3083, transformed to EPSG:4326 and rounded to 6 decimals (about 0.1 m). A centroid is not
   used because it can fall outside a concave or multi-part district or county; a representative
   point is always inside. A test checks every real point, projected back, lies inside its
   polygon.
5. **Precinct to district.** Per plan, the district whose intersection area with the precinct is
   largest (an STRtree finds candidates); areas within a relative 1e-9 of the largest tie, and a
   tie goes to the lower district number. `*_share` is that area over the precinct's area, rounded
   to 4 decimals. `split_* = share < 0.99` on the rounded share: true means at least 1% of the
   precinct lies outside its assigned district, so the district is right for most but not all of
   its voters. The share is over the whole precinct, so area that falls outside every district of
   the plan (a coastline or a boundary mismatch) also counts against it: `split` means the largest
   district covers under 99% of the precinct, not strictly that two districts share it. A precinct with no overlap gets NULL district, share and flag. Voterfile district
   columns are not used.
6. **Determinism.** No timestamps; areas sorted by `(kind, code)`, precincts by `pctkey`; sorted
   keys; fixed rounding; one record per line. Identical inputs and library versions give
   byte-identical output. Provenance records the pipeline submodule commit (the gitlink in HEAD),
   the plan names, the precinct file and its year and kind, the sha256 of every source file, the
   method, the EPSG codes and the library versions.
7. **Tables.** `geo_area (kind, code)`, `geo_precinct (pctkey)`, `geo_precinct_alias (county,
   precinct)` and `geo_meta (name)`, as SQLModel models in the loader-only `geo_models.py`. The
   DDL and SQL are committed in `geo_tables.py` (no sqlmodel) and pinned by a drift test, the
   ADR-009 pattern. `early_vote_sync` creates them with the other tables. Codes: county is the
   3-digit FIPS, districts are plain integers as text, precincts are the state `PCTKEY`.
8. **Load.** `geo_load.load_geo` skips when `geo_meta.sha256` equals the file's sha256. Otherwise it
   validates every row, upserts in bounded `json_each` batches, deletes rows the file no longer
   has, and writes `geo_meta` last, so an interrupted load is redone in full next run.
9. **Aliases.** `geo_load.build_aliases` reads the distinct live `(county, precinct)` pairs of
   `early_vote_roster_row` (one day and one row-hash quarter per query), finds the county's FIPS
   by its letters only, and tries the precinct text exact, zero-padded to 4, then alphanumerics
   only zero-padded to 4, always within that county. A pair that matches none is stored as
   `unmatched` with no `pctkey`. Only changed alias rows are written; pairs no longer in the
   roster are deleted. Logs carry counts only.

## Why the shapefiles are not in the submodule

`vendor/voterfile-audit-pipeline` is pinned for provenance and its fetch scripts. That repo
gitignores `data/reference/*`, so the submodule has no layers; its setup downloads them into a
working checkout. The layers are about 300 MB, well past what this repo should carry, and the
committed JSON (about 2.6 MB) is all the loader needs.

## Reproducing the committed JSON

```bash
uv sync --extra geo
uv run python -m texas_turnout_scraper.geo_build \
  --reference-dir /path/to/voterfile-audit-pipeline/data/reference --out data/geo/tx_geo.json
```

The reference directory defaults to `$TX_REFERENCE_DIR`, then
`vendor/voterfile-audit-pipeline/data/reference`. With the same source files (their sha256 are in
the JSON's provenance) and the same library versions, the output is byte-identical.

## Consequences

- A plan change means replacing the plan files, rebuilding and committing the JSON; the next
  scheduled run loads it and deletes rows the old plan had (RUNBOOK).
- Measured on the roster before this change, 9,272 of 10,888 pairs (99.3% of rows) resolve. The rest are kept as
  `unmatched` for review, never guessed.
- 26 precincts are split in at least one plan (6 congressional, 5 state senate, 16 state house).
- Library versions are part of the provenance, so a shapely, GEOS or PROJ upgrade can change the
  last digit of a point and the bytes of the file. The rebuild shows that as a diff to review.

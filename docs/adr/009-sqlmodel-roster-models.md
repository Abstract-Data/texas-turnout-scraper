# ADR-009: SQLModel models for roster rows written to D1

**Date:** 2026-10-23
**Status:** Accepted (narrows ADR-002 for the D1 roster-row loader only)

## Context

The early-vote loader stores every day's statewide roster in the election's D1 database as
rows (`early_vote_roster_row`), keyed by file version. The first version of that loader used a
hand-written `CREATE TABLE`, a `NamedTuple` per row, and no validation. A bad VUID or an unknown
voting method went into D1 as is, and columns the loader did not know about were dropped.

ADR-002 keeps the package's public output models as plain Pydantic so consumers own persistence.
That still holds for everything consumers import. The D1 loader is different: we own that
schema, and it needs one place that states each field, its rule, and its table.

## Decision

1. `src/texas_turnout_scraper/roster_models.py` defines the rows written to D1 as SQLModel
   models: `RosterRow` (one voter row), `RosterReject` (a quarantined row), and
   `RosterRowsMarker` (the per-version marker written last). The models carry the field rules and
   generate the DDL.
2. D1 is reached through the Cloudflare HTTP API, so there is no SQLAlchemy engine or session. The
   models give schema, validation, and documentation, not queries. The generated DDL is committed
   as constants in `early_vote_roster_rows.py`, and `tests/unit/test_roster_models.py` regenerates
   it from the models to catch drift.
3. Ingest always calls `Model.model_validate(...)`. A `table=True` constructor skips validation
   (a test pins this), so it is never used on file data.
4. Capture everything. Every column of the day file reaches D1: known columns map to fields, every
   other column goes to the row's `extra` JSON object keyed by its header text, empty values kept.
   This includes polling-place, vote-site, and ballot-style columns. It overrides the SOS Advisory
   2024-20 allowlist that `early_vote_roster.py` used to apply. This was the owner's call.
5. Rows that fail validation are not dropped. They go to `early_vote_roster_reject` with the
   complete original row and a reason built from field names and error types only. The day still
   loads with its valid rows. A header that cannot be mapped (a required column missing, or two
   headers equal after strip and upper-case) stops the load before anything is written.
6. Scope is loader-only. `sqlmodel` is in the `loader` extra and the `dev` group, not in core
   dependencies. `early_vote_sync` and everything the Worker imports must not import sqlmodel at
   import time; the loader imports `roster_models` inside the functions that need it.

## Consequences

- Canonicalization (`canonicalize_roster_csv`) now keeps all columns and re-serializes with minimal
  quoting. For the current five-column portal file the bytes, and so the stored hashes, are the
  same as under the old redaction. A golden test holds that.
- R2 keeps the byte-exact canonical CSV. Validation only decides which table a row lands in.
- The Worker bundle stays free of sqlmodel and SQLAlchemy (about 25 MiB), and a test asserts the
  sync module does not import them.
- Risks. A future portal column holding something sensitive is now stored, not filtered. Polling
  locations in D1 and in served CSVs are a policy change from the advisory allowlist. D1 tables
  created by an earlier build of this loader lack `extra` and `reject_count`; `CREATE TABLE IF NOT
  EXISTS` will not add them. The two SQLAlchemy versions in `uv.lock` (2.0 and 2.1, split by Python
  version) generate the same SQLite DDL today; the drift test fails first if that changes.

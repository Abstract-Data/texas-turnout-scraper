# RUNBOOK — texas-turnout-scraper

Operational quick reference for local development and live verification.

## Setup

```bash
uv sync --dev
```

## Type check and lint

```bash
uv run ty check
uv run ruff check . --fix && uv run ruff format .
```

## Tests

```bash
# Fast unit tests (mocked HTTP — http_backend="httpx" + respx)
uv run pytest tests/unit -q

# Integration tests skipped unless --live is passed
uv run pytest tests/integration/ -q

# Live legacy/Civix portal checks (network + cloudscraper default)
uv run pytest tests/integration/ -v --live

# AGENTS.md structural verify
uv run pytest tests/verify -q
```

## CLI examples

```bash
uv run tx-turnout civix elections
uv run tx-turnout legacy elections
uv run tx-turnout legacy turnout 49664 2024-10-21
uv run tx-turnout legacy roster 49664 2024-10-21 --strategy A
uv run tx-turnout legacy roster 49664 2024-10-21 --strategy B --out-dir ./tmp
uv run tx-turnout audit run-inline path/to/roster.csv
```

## Legacy SOS session flow

The pre-2025 portal is stateful (Java/Struts). Always:

1. **GET** `getElectionDetails.do` → `JSESSIONID`
2. **POST** `getElectionEVDates.do` → prime election (`LegacySession.prime_election`)
3. **POST** `getEVDetails.do` or `downloadVoterInfoReport.do` → data

Use `legacy_api` facades from CLI/MCP — they manage the session and priming.

## HTTP backend

- **Production / live tests:** `http_backend="cloudscraper"` (default on `LegacySession`)
- **Unit tests:** `http_backend="httpx"` so respx can mock requests

## Releases (Release Please)

Version and `CHANGELOG.md` are managed on `main` by the **Release Please** workflow
(`.github/workflows/release-please.yml`). After merges that use [Conventional Commits](https://www.conventionalcommits.org/),
the bot opens or updates a **release PR**. Merging that PR bumps `pyproject.toml`, updates the changelog, creates a
`v*` tag, and publishes a GitHub Release.

```bash
# Manual trigger (maintainers)
gh workflow run release-please.yml
```

- Use `feat:`, `fix:`, `docs:`, `chore:`, etc. on `main` for library changes.
- Scheduled `data: refresh ...` commits are ignored for semver; they do not block releases.
- See [ADR-005](docs/adr/005-release-please.md) for rationale.
- **`uv.lock` is bumped by the release PR.** Release Please has no `uv.lock` updater for Python, so
  `release-please-config.json` carries an `extra-files` entry that rewrites the project's own
  `[[package]]` version by index (`$.package[62].version`). The TOML updater cannot filter by name.
  The index shifts when dependencies are added or removed before `texas-turnout-scraper` in `uv.lock`.
  `tests/verify/check_release_please_uv_lock.py` fails in CI and prints the new index when that happens;
  update the number in the config. Without this, the release PR fails `uv sync --locked`.
- The Claude Code Review workflow skips `release-please--*` PRs (bot-authored, generated).

## Common issues

| Symptom | Fix |
|---------|-----|
| Legacy CLI `ImportError` on turnout/roster | Use `legacy_api` exports (`fetch_county_turnout`, `fetch_roster`) |
| Unit tests hit live SOS | Ensure tests construct `LegacySession(http_backend="httpx")` and use `@respx.mock` |
| Turnout/roster empty after code change | Confirm `prime_election()` ran before `getEVDetails.do` |
| Integration tests always skipped | Pass `--live` to pytest |
| Live tests fail with 403 | WAF — confirm cloudscraper backend, not raw httpx |

### Early-vote day versions and change log

When a day's file changes on reload, the loader first writes both the old and new versions to the private R2 bucket `tx-turnout-ev-snapshots-preview` (`elections/<id>/<ev_date>/<sha256>.csv.gz`, plus a `manifest.json` per day), then logs the voters added and removed in the D1 table `early_vote_day_change` (VUID, county and voting method only, never names). R2 expires snapshots after 90 days. If the R2 write fails the load fails before D1 is touched, so no version is lost. The loader uses `EV_SNAPSHOT_R2_ACCESS_KEY_ID` and `EV_SNAPSHOT_R2_SECRET_ACCESS_KEY` from the 1Password environment when set, otherwise the shared `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` (which must have Object Read and Write on this bucket); with neither it skips R2 and still logs the diff. The bucket `tx-turnout-ev-snapshots-preview` and its lifecycle rules (delete `elections/` objects after 90 days, abort multipart uploads after 1 day) were created by hand through the Cloudflare API, not Terraform, because the Terraform token has no R2 permission. Recreate them the same way if the bucket is ever lost. Versions from before this feature, such as the 10-14 file replaced on 10-23, were not kept.

### Early-vote loader: why scheduled runs use `backfill`

The Civix portal's per-date files for an election are **daily** (consecutive days share no voter IDs) and a day's file can gain late records after it is first published. The scheduled `early-vote-sync.yml` therefore runs `sync_mode=backfill`: it re-fetches every day, hashes each file, skips days whose hash matches what is stored, rewrites days that changed, and bumps the KV version only when something changed. `sync_mode=latest` (dispatch only) looks at today's file alone and will not repair earlier days. If a day is missing from the API, check that the scheduled runs were approved and not cancelled; a dry-run `backfill` dispatch shows what the portal serves.

### Early-vote roster rows (`early_vote_roster_row`)

Each day's roster rows are stored in the election's D1 under the key `(day, row_hash, occ)`. `day` is the date's ordinal (`date.fromisoformat(ev_date).toordinal()`), `row_hash` is a signed 64-bit BLAKE2b of the file header and the row's cells, and `occ` numbers identical lines within the file (0, 1, ...). Every column of the file is kept: `county`, `voter_name`, `id_voter`, `voting_method` and `precinct` are columns, and every other header (polling place, vote site, ballot style, anything new) is in the row's `extra` JSON object under its header text, empty values included. See `docs/adr/009-sqlmodel-roster-models.md`.

**Versions.** A day's rows carry `added_in` and `removed_in` (NULL while live). The day's current version is one row in `early_vote_roster_version` (`version`, `file_id`, `sha256`, `row_count`, `reject_count`). Rows visible at version V are `added_in <= V AND (removed_in IS NULL OR removed_in > V)` (`VISIBLE_AT_SQL` in `early_vote_roster_rows.py`). When a day file changes, the loader hashes the new file and the file the current version was built from, inserts only the added lines at V+1, marks only the removed lines `removed_in = V+1`, counts what V+1 shows, then writes the version row (the flip), and then the day pointer. A reader pinned to V sees the old file until the flip. Unchanged lines are never sent. A file whose lines all match the current version writes only the version row (same version, new `file_id` and `sha256`).

**Recovery and collection.** Each load first deletes rows added after the current version and clears `removed_in` marks after it, so a failed attempt leaves nothing visible and nothing behind. After the flip it deletes rows with `removed_in <= V+1`. Both run as 16 key-range statements per table. If the file the current version was built from is gone (its chunks were deleted), the load retires every live row at V+1 and inserts the whole file; rows still in the new file are revived in place and stay visible to V readers.

**Migration.** The first loader keyed rows by `file_id` (tables `early_vote_roster_row`, `early_vote_roster_reject`, `early_vote_roster_rows`). On the first schema check against a database that still has that shape, the loader first deletes the two large ones (`early_vote_roster_row`, `early_vote_roster_reject`) in slices of 50,000 rows, because one DROP of about a gigabyte could exceed D1's statement time limit and would then fail every later run. It then drops the three tables and creates the new ones. Each slice is committed on its own, so a run that stops partway resumes from what is left on the next call; a timeout during the drain is safe to rerun. Roster rows are only written by the loader, so nothing readable is lost: the rows come back from the chunks. The backfill step below rebuilds them. Expect the first scheduled run after deploy to rewrite every day once.

**Rejects.** A row that fails validation (blank county or name, a VUID that is not 1 to 10 digits, a voting method other than `IN-PERSON` or `MAIL-IN`, or a cell count that differs from the header) goes to `early_vote_roster_reject` with the whole original row in `raw` (header text to cell, plus `_overflow` for surplus cells) and a `reason` such as `id_voter:value_error` or `column_count`. Rejects follow the same keys and versions as rows. The day still loads. The loader logs `version=V rows=N rejects=M added_rows=A added_rejects=R removed=D` per day, counts only. To read a day's counts and rejects:

```sql
SELECT version, row_count, reject_count FROM early_vote_roster_version WHERE ev_date = '2026-10-19';
SELECT reason, COUNT(*) FROM early_vote_roster_reject
WHERE day = CAST(julianday('2026-10-19') - 1721424.5 AS INTEGER)
  AND added_in <= :v AND (removed_in IS NULL OR removed_in > :v)
GROUP BY reason;
```

`row_count + reject_count` is the file's data-line count at that version. The loader checks it against a count of visible rows before the flip. If they differ, the stored rows no longer match the file, so the load raises `EarlyVoteSyncError` (counts only) and does not flip the version. The day keeps serving its previous version, and the next run clears the unpublished marks and tries again. A repeating mismatch means the day's rows were edited outside the loader. A sudden jump in rejects usually means the portal changed a column's format.

**Hard stop.** A header missing `COUNTY`, `VOTER_NAME`, `ID_VOTER` or `VOTING_METHOD`, or with two headers that are equal after trimming and upper-casing, fails the day with `RosterSchemaError` before anything is written, so the previous version stays live. The error names the columns, never row content. The backfill skips such a file with a warning. A header change that passes the check changes every key, so the next load replaces every row of that day.

**Backfill.** The `Backfill roster rows` step in `early-vote-sync.yml` (`python -m texas_turnout_scraper.early_vote_roster_rows_backfill --election-id <id> --dry-run|--apply`) loads every day whose version row is missing or names a different file than `early_vote_day`, through the same day load as the loader. Reruns write nothing. It prints `rows_written` and `rejects` (rows and rejects inserted). Check a day with `row_count` from `early_vote_roster_version` against its `early_vote_file_aggregate.statewide_total`. The two can differ by rows the aggregates skip (empty county or an unrecognized voting method), which are the rejects.

The loader needs the `loader` extra (`uv sync --extra loader`; the `dev` group includes it). The Worker does not.

### An election has stopped updating on Pages (held back for size)

`data-refresh.yml` runs `.github/scripts/skip_oversize_data.py` before it commits. GitHub rejects any single file over 100 MiB, so a file over 95 MiB holds back its whole election: the election folder goes back to its last published state (or is removed if it was never published) and its `index.json` entry is restored. Other elections still publish.

- Where to look: the run's **Summary** page has a "Held back" table, and the log has `::warning::` lines naming the file and size.
- What it means: that election is frozen on Pages at its last published data. A statewide roster for election 53813 was 130.79 MB when this was added, so it is not published.
- To publish it anyway you need a different format or host for the file (for example gzip or LFS). That is a design decision, not a runbook step.
- If `index.json` is not valid JSON in the working tree, the step fails with an `::error::` annotation. Fix the refresh that wrote it.

## PII

Never log `VOTER_NAME` or `ID_VOTER`. Use record counts in log messages only.

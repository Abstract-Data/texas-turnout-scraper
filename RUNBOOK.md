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

Each day's roster rows are stored in the election's D1, keyed by the day file's `file_id` and numbered in file order (`row_id`). Every column of the file is kept: `county`, `voter_name`, `id_voter`, `voting_method` and `precinct` are columns, and every other header (polling place, vote site, ballot style, anything new) is in the row's `extra` JSON object under its header text, empty values included. See `docs/adr/009-sqlmodel-roster-models.md`.

The loader inserts a version's rows and rejects, then writes its `early_vote_roster_rows` marker (`row_count`, `reject_count`), then moves the day pointer, and finally deletes the replaced version (marker, then rejects, then rows, then chunks). Rows without a marker are invisible, so a failed load leaves nothing a reader can see. The next loader run drops the rows and rejects of any version no day points to (it finds them through the chunk, file and marker tables), and the backfill step rewrites rows for a file that still has no marker. The chunk store and R2 stay the source of truth for the hash and the byte-exact CSV.

**Rejects.** A row that fails validation (blank county or name, a VUID that is not 1 to 10 digits, a voting method other than `IN-PERSON` or `MAIL-IN`, or a cell count that differs from the header) goes to `early_vote_roster_reject` with the whole original row in `raw` (header text to cell, plus `_overflow` for surplus cells) and a `reason` such as `id_voter:value_error` or `column_count`. The day still loads. The loader and backfill log `rows=N rejects=M` per file, counts only. To look at a day's rejects:

```sql
SELECT reject_count FROM early_vote_roster_rows WHERE file_id = '<file_id>';
SELECT reason, COUNT(*) FROM early_vote_roster_reject WHERE file_id = '<file_id>' GROUP BY reason;
```

`row_count + reject_count` is the file's data-line count. A sudden jump in rejects usually means the portal changed a column's format.

**Hard stop.** A header missing `COUNTY`, `VOTER_NAME`, `ID_VOTER` or `VOTING_METHOD`, or with two headers that are equal after trimming and upper-casing, fails the day with `RosterSchemaError` before anything is written, so the previous version stays live. The error names the columns, never row content. The backfill skips such a file with a warning.

Days stored before this table existed get their rows from the `Backfill roster rows` step in `early-vote-sync.yml` (`python -m texas_turnout_scraper.early_vote_roster_rows_backfill --election-id <id> --dry-run|--apply`). It skips files that already have a marker, so reruns write nothing, and prints `rows_written` and `rejects`. The first run for a full election writes about 2.25M rows and takes on the order of 15 minutes. Check a day with `SELECT row_count FROM early_vote_roster_rows` against its `early_vote_file_aggregate.statewide_total`. The two can differ by rows the aggregates skip (empty county or an unrecognized voting method), which are now the rejects.

The loader needs the `loader` extra (`uv sync --extra loader`; the `dev` group includes it). The Worker does not.

### An election has stopped updating on Pages (held back for size)

`data-refresh.yml` runs `.github/scripts/skip_oversize_data.py` before it commits. GitHub rejects any single file over 100 MiB, so a file over 95 MiB holds back its whole election: the election folder goes back to its last published state (or is removed if it was never published) and its `index.json` entry is restored. Other elections still publish.

- Where to look: the run's **Summary** page has a "Held back" table, and the log has `::warning::` lines naming the file and size.
- What it means: that election is frozen on Pages at its last published data. A statewide roster for election 53813 was 130.79 MB when this was added, so it is not published.
- To publish it anyway you need a different format or host for the file (for example gzip or LFS). That is a design decision, not a runbook step.
- If `index.json` is not valid JSON in the working tree, the step fails with an `::error::` annotation. Fix the refresh that wrote it.

## PII

Never log `VOTER_NAME` or `ID_VOTER`. Use record counts in log messages only.

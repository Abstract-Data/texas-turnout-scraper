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

The Civix portal's per-date files for an election are **daily** (consecutive days share no voter IDs) and a day's file can gain late records after it is first published. The scheduled `early-vote-sync.yml` therefore runs `sync_mode=backfill`: it re-fetches every day, hashes each file, skips days whose hash matches what is stored, rewrites days that changed, and bumps the KV version only when something changed. `sync_mode=latest` (dispatch only) looks at today's file alone and will not repair earlier days. If a day is missing from the API, check that the scheduled runs succeeded and were not cancelled; a dry-run `backfill` dispatch shows what the portal serves.

### Early-vote schedule (Worker cron)

The preview Worker's cron trigger dispatches `early-vote-sync.yml` at 10:00, 17:00 and 22:00 Central until 11:59 PM on 2026-11-03 (`docs/adr/013-worker-cron-dispatches-sync.md`). Those runs have `trigger=cron` and apply in the `preview-scheduled` environment with no reviewer. Manual dispatches keep `trigger=manual` and the `preview` reviewer gate.

- **See a run.** GitHub Actions → Early vote sync: cron runs are `workflow_dispatch` runs whose `trigger` input is `cron`. The Worker side is in Workers Logs for `tx-turnout-ev-api-preview`: a cron invocation logs `early-vote dispatch accepted: HTTP 204`, `early-vote dispatch skipped: GITHUB_DISPATCH_TOKEN is not set`, or fails with `DispatchError` and the HTTP status. Hours outside 10, 17 and 22 Central log nothing.
- **Stop it now.** Revoke the fine-grained token in GitHub (Settings → Developer settings → Fine-grained tokens). Dispatches then fail with 401 and nothing runs. To stop it for good, remove `cloudflare_workers_cron_trigger.ev_sync` from `infra/preview/main.tf` and apply through the Terraform workflow. Trigger changes take up to 15 minutes to reach Cloudflare.
- **It ends by itself.** After 2026-11-04 05:59:59 UTC the trigger still fires but sends nothing.
- **Set or rotate the token.** Create a fine-grained token for `Abstract-Data/texas-turnout-scraper` with only Actions: write, store it as the `preview` environment secret `EV_DISPATCH_GITHUB_TOKEN`, and run the Terraform workflow. The Worker binding `GITHUB_DISPATCH_TOKEN` exists only when that secret is non-empty. Never log or print the token.
- **`preview-scheduled` environment.** Settings → Environments: no required reviewers, deployment branches `main` only, and the same secrets as `preview` (`OP_SERVICE_ACCOUNT_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`). This is a settings step; the repository does not configure it.

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

### Geography tables (`geo_area`, `geo_precinct`, `geo_precinct_alias`, `geo_meta`)

`data/geo/tx_geo.json` holds a point (`lat`, `lon`) inside every county, state precinct, and congressional, state senate and state house district, plus each precinct's district per plan with its overlap `*_share` and `split_*` flag (`split` means the largest district covers less than 99% of the precinct, which includes area outside every district of the plan). The `Load geography and precinct aliases` step in `early-vote-sync.yml` loads it into the election's D1 and maps roster precincts to state precincts. See `docs/adr/010-geography-tables.md`.

**Rebuild after a plan or precinct change.** The layers come from the voterfile-audit-pipeline checkout's `data/reference/` (its setup downloads them; the submodule at `vendor/voterfile-audit-pipeline` holds only the fetch scripts). Each district folder (`tiger/congressional`, `tiger/state_senate`, `tiger/state_house`) and `tiger/county` must hold exactly one `.shp`; the precinct file is the one `tx_election/precincts/current.json` names. Replace the old plan's files with the new ones (the build refuses a folder with two plans), update `EXPECTED_COUNTS` in `geo_build.py` if a plan's district count changed, then:

```bash
uv sync --extra geo
uv run python -m texas_turnout_scraper.geo_build \
  --reference-dir /path/to/voterfile-audit-pipeline/data/reference --out data/geo/tx_geo.json
TX_REFERENCE_DIR=/path/to/voterfile-audit-pipeline/data/reference uv run pytest tests/unit/test_geo_build.py -q
```

The build takes about 40 seconds and logs the byte size, sha256, counts per kind, split precincts per plan and precincts with no overlap. The last test checks every point lies inside its polygon. A rebuild from the same files is byte-identical, so a diff in the JSON is a real change. Commit the JSON; the next scheduled run loads it (rows the file no longer has are deleted, `geo_meta` is written last). An unchanged file writes nothing. If a source file disagrees with the pipeline's `.checksums.json`, the build stops: refetch the layer.

**Reading the alias report.** The step logs `pairs`, `matched_pairs`, `unmatched_pairs`, `rows`, `matched_rows`, `matched_row_share` and the pair count per rule (`exact`, `zfill4`, `alnum_zfill4`, `unmatched`), counts only. Roster `COUNTY` is compared with county names by letters only (`DE WITT` = `DeWitt`); the precinct text is tried as is, zero-padded to 4, then with non-alphanumerics removed and zero-padded to 4, always within the same county. A pair that matches none is stored with `rule = 'unmatched'` and no `pctkey`; nothing is guessed. When these tables were designed, about 99.3% of roster rows matched. To list the unmatched pairs:

```sql
SELECT county, precinct FROM geo_precinct_alias WHERE rule = 'unmatched' ORDER BY county, precinct;
SELECT rule, COUNT(*) FROM geo_precinct_alias GROUP BY rule;
```

A sharp drop in `matched_row_share` after a precinct rebuild usually means a county renumbered its precincts or the portal changed the precinct format.

### Voter attributes (`early_vote_voter`, `early_vote_voter_age`, `early_vote_election_calendar`, `age_tier`)

For every voter in the election's live roster, `early_vote_voter` holds `sex`, `dob`, `edr`, the 17 history codes, the early day they voted in the 2025 amendment election and the 2026 primary and runoff, and derived scores. `early_vote_voter_age` gives their age and age tier today and on the current election day. Nothing in the Worker reads these yet. See `docs/adr/011-voter-attributes.md`.

**Run it locally, never in CI.** The voterfile is PII and stays on the owner's machine. The early-vote sync creates the tables; the link fills them:

```bash
export TX_VOTERFILE="/path/to/texas2026september.csv"   # or pass --voterfile
# Cloudflare credentials as for the sync: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, CLOUDFLARE_KV_NAMESPACE_ID
uv run python -m texas_turnout_scraper.voter_link --election-id 53815 --dated-history --dry-run
uv run python -m texas_turnout_scraper.voter_link --election-id 53815 --dated-history --apply
```

A dry run reads D1 and the voterfile, fetches the dated days, and reports; it creates and writes nothing. `--apply` writes only voters whose `src_hash` changed, then the `early_vote_voter_meta` rows last; a rerun with the same inputs writes nothing. Voters are never deleted.

**Dated history.** `--dated-history` fetches every early day of elections 51031, 53813, 53814, 58314 and 58315 from the Civix portal (about 44 files, paced at 1 s or more) and caches each under `--cache-dir` (default `~/.cache/texas-turnout-scraper/statewide`) as `<civix id>/<date>.<sha256>.bin`. A rerun reads the cache and fetches nothing. The cached files hold names and VUIDs: keep them on the local machine and delete the folder when done. If the portal's dates or day counts disagree with `ELECTION_CALENDAR` in `voter_tables.py`, the run logs a `calendar` warning (counts and dates only); fix the constant and its test before trusting the day columns.

**Reading the report.** Counts only: `targets` (live roster VUIDs), `matched` and `matched_share` (found in the voterfile), `unmatched`, `rejects` (an unknown history code or sex; the row is kept in `early_vote_voter_reject` with a value-free reason), `invalid_dob` and `invalid_edr` (stored as NULL, `flags` bit 1 or 2), `written`, `unchanged`, `pending` (what an apply would write) and `dated_<KEY>` (target voters found in that election's day files).

**After a scoring change**, bump `SCORE_VERSION` in `voter_tables.py`. It is part of `src_hash`, so the next apply rewrites every voter.

The voterfile must hold each VUID once (the September 2026 file does: 18,932,927 rows, 18,932,927 distinct). If a file repeats a VUID, the link stops with `voterfile repeats a VUID; nothing marked complete` before the meta rows are written, because which row would win depends on read order. Fix the file and rerun.

### Repeat VUIDs (`early_vote_vuid_repeat`, `early_vote_repeat_excess`, `early_vote_repeat_meta`)

The roster can list one voter more than once. On 2026-10-24 we observed that every repeated VUID
appeared exactly twice. In most cases the second listing was early day 11 (2026-10-24), whose file
re-lists voters from earlier days, mostly day 6. That is an observation about the portal's files,
not a known cause. The portal served exactly the file we stored. See
`docs/adr/012-repeat-vuids.md`.

**What the tables mean.**

- `early_vote_vuid_repeat` has one row per VUID that the live roster counts more than once, or
  that the change log (`early_vote_day_change`, `change_kind = 'removed'`) shows removed at least
  once. `counted_days` and `removed_days` are distinct dates ascending. `appearances` lists every
  live row in first-to-last order. `flags` is 1 for a same-day repeat, 2 for a cross-day repeat, 4
  for a cross-method repeat and 8 for removed at least once. A VUID with no row was counted exactly
  once and never removed.
- `early_vote_repeat_excess` gives, per day, county and voting method, how many live rows there
  are not the voter's first appearance.
- `early_vote_repeat_meta` holds the build signature and the totals.

**First-appearance rule.** A voter belongs to their earliest day. Within one day they belong to
the row with the lowest `(row_hash, occ)` key. Every other live row of that voter is excess.

**Reading the counts.** Raw row counts stay as they are. The distinct voters of any slice are
`raw rows in the slice - excess in the slice`. For example, distinct voters on a day are that day's
live rows minus the day's `SUM(n)` in `early_vote_repeat_excess`. `raw_rows - excess_rows` in
meta equals `distinct_voters`.

**Running it.** The scheduled sync runs it after the geography step. By hand:

```bash
# Cloudflare credentials as for the sync: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, CLOUDFLARE_KV_NAMESPACE_ID
uv run python -m texas_turnout_scraper.repeat_build --election-id 53815 --dry-run
uv run python -m texas_turnout_scraper.repeat_build --election-id 53815 --apply
```

An apply whose signature matches the stored one reports `status=unchanged` and reads no roster
rows. The signature covers the roster version rows and the change log's row count and last
`recorded_at`. Otherwise the build reads every live row, writes only rows whose `src_hash`
changed, deletes rows that no longer qualify, and writes meta last. An interrupted apply is redone
in full by the next run. A dry run reports `rows_pending`, `rows_stale`, `excess_pending` and
`excess_stale` and writes nothing. After a change to how rows are derived, bump `REPEAT_VERSION`
in `repeat_tables.py` so the next apply rebuilds.

### Voter geography (`voter_geo`, `voter_geo_meta`)

One row per VUID with a live roster row: `county_fips`, `pctkey`, `cd`, `sd`, `hd`, `split_flag`
(bits 1 cd, 2 sd, 4 hd) and `flags` (1 more than one precinct, 2 more than one county). Values come
from the voter's first appearance and the geography tables. See `docs/adr/015-voter-geography.md`.

**Not in the scheduled sync.** The first build writes about one row per distinct live VUID (about
3.68M for 53815), which does not fit the 5-minute workflow cap. Run the first apply by hand, only
with the owner's approval. Load geography first (`geo_load`), because the build reads the stored
aliases.

```bash
# Cloudflare credentials as for the sync: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, CLOUDFLARE_KV_NAMESPACE_ID
uv run python -m texas_turnout_scraper.voter_geo_build --election-id 53815 --dry-run
uv run python -m texas_turnout_scraper.voter_geo_build --election-id 53815 --apply
```

The output is counts only. `matched_row_share` is the share of live rows whose pair has a state
precinct, the figure ADR-010 measured at 99.3%. `multi_precinct_vuids` and `multi_county_vuids`
count voters whose listings disagree; they take their first appearance. An apply whose signature
matches reports `status=unchanged` and writes nothing. After a change to how rows are derived, bump
`VOTER_GEO_VERSION` in `voter_geo_tables.py`.

**Reading it.** `GET /elections/{id}/roster/{ev_date}/geo?page=0` through `63` with a roster-scoped
token. The pages partition the day's live VUIDs. A 503 `voter_geo_not_loaded` means the tables do
not exist yet.

### An election has stopped updating on Pages (held back for size)

`data-refresh.yml` runs `.github/scripts/skip_oversize_data.py` before it commits. GitHub rejects any single file over 100 MiB, so a file over 95 MiB holds back its whole election: the election folder goes back to its last published state (or is removed if it was never published) and its `index.json` entry is restored. Other elections still publish.

- Where to look: the run's **Summary** page has a "Held back" table, and the log has `::warning::` lines naming the file and size.
- What it means: that election is frozen on Pages at its last published data. A statewide roster for election 53813 was 130.79 MB when this was added, so it is not published.
- To publish it anyway you need a different format or host for the file (for example gzip or LFS). That is a design decision, not a runbook step.
- If `index.json` is not valid JSON in the working tree, the step fails with an `::error::` annotation. Fix the refresh that wrote it.

## PII

Never log `VOTER_NAME` or `ID_VOTER`. Use record counts in log messages only.

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

### Early-vote loader: why scheduled runs use `backfill`

The Civix portal's per-date files for an election are **daily** (consecutive days share no voter IDs) and a day's file can gain late records after it is first published. The scheduled `early-vote-sync.yml` therefore runs `sync_mode=backfill`: it re-fetches every day, hashes each file, skips days whose hash matches what is stored, rewrites days that changed, and bumps the KV version only when something changed. `sync_mode=latest` (dispatch only) looks at today's file alone and will not repair earlier days. If a day is missing from the API, check that the scheduled runs were approved and not cancelled; a dry-run `backfill` dispatch shows what the portal serves.

### An election has stopped updating on Pages (held back for size)

`data-refresh.yml` runs `.github/scripts/skip_oversize_data.py` before it commits. GitHub rejects any single file over 100 MiB, so a file over 95 MiB holds back its whole election: the election folder goes back to its last published state (or is removed if it was never published) and its `index.json` entry is restored. Other elections still publish.

- Where to look: the run's **Summary** page has a "Held back" table, and the log has `::warning::` lines naming the file and size.
- What it means: that election is frozen on Pages at its last published data. A statewide roster for election 53813 was 130.79 MB when this was added, so it is not published.
- To publish it anyway you need a different format or host for the file (for example gzip or LFS). That is a design decision, not a runbook step.
- If `index.json` is not valid JSON in the working tree, the step fails with an `::error::` annotation. Fix the refresh that wrote it.

## PII

Never log `VOTER_NAME` or `ID_VOTER`. Use record counts in log messages only.

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

## Common issues

| Symptom | Fix |
|---------|-----|
| Legacy CLI `ImportError` on turnout/roster | Use `legacy_api` exports (`fetch_county_turnout`, `fetch_roster`) |
| Unit tests hit live SOS | Ensure tests construct `LegacySession(http_backend="httpx")` and use `@respx.mock` |
| Turnout/roster empty after code change | Confirm `prime_election()` ran before `getEVDetails.do` |
| Integration tests always skipped | Pass `--live` to pytest |
| Live tests fail with 403 | WAF — confirm cloudscraper backend, not raw httpx |

## PII

Never log `VOTER_NAME` or `ID_VOTER`. Use record counts in log messages only.

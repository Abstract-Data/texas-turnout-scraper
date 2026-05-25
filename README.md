# texas-turnout-scraper

Python library, CLI (`tx-turnout`), and MCP server for Texas Secretary of State early-voting turnout and voter roster data.

## Install

```bash
uv sync --dev
```

## CLI

```bash
# Civix EVR (2025+)
uv run tx-turnout civix elections
uv run tx-turnout civix turnout 53813 2025-03-01

# Legacy SOS portal (pre-2025)
uv run tx-turnout legacy elections
uv run tx-turnout legacy turnout 49664 2024-10-21
uv run tx-turnout legacy roster 49664 2024-10-21
uv run tx-turnout legacy roster 49664 2024-10-21 --strategy B --out-dir ./data/tmp

# Audit and voterfile match
uv run tx-turnout audit run-inline data/elections/49664/roster_2024-10-21.csv
uv run tx-turnout voterfile match roster.csv /path/to/voterfile.csv
```

## Tests

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration/ -q          # skipped by default
uv run pytest tests/integration/ -v --live   # hits live SOS/Civix APIs
```

Integration tests require `--live` and network access. Unit tests mock HTTP with respx and `http_backend="httpx"`.

## Documentation

- [`AGENTS.md`](AGENTS.md) — agent constraints and layout
- [`RUNBOOK.md`](RUNBOOK.md) — dev commands and legacy session flow
- [`docs/ARCHITECTURE_SPEC.md`](docs/ARCHITECTURE_SPEC.md) — design source of truth
- [`docs/EARLY_VOTING_ROSTER.md`](docs/EARLY_VOTING_ROSTER.md) — legacy HTTP reference

## Data

Cached election data lives under `data/elections/` and is refreshed on a schedule (GitHub Actions). Consumers pull from GitHub Pages static JSON/CSV — not a real-time scrape endpoint.

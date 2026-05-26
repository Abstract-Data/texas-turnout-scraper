# texas-turnout-scraper

Python library, CLI (`tx-turnout`), and MCP server for Texas Secretary of State early-voting turnout and voter roster data.

[![CI](https://github.com/Abstract-Data/texas-result-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/Abstract-Data/texas-result-scraper/actions/workflows/ci.yml)
[![Data Refresh](https://github.com/Abstract-Data/texas-result-scraper/actions/workflows/data-refresh.yml/badge.svg)](https://github.com/Abstract-Data/texas-result-scraper/actions/workflows/data-refresh.yml)
[![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

### Runtime dependencies

[![httpx](https://img.shields.io/pypi/v/httpx?label=httpx)](https://pypi.org/project/httpx/)
[![Pydantic](https://img.shields.io/pypi/v/pydantic?label=pydantic)](https://pypi.org/project/pydantic/)
[![Typer](https://img.shields.io/pypi/v/typer?label=typer)](https://pypi.org/project/typer/)
[![MCP](https://img.shields.io/pypi/v/mcp?label=mcp)](https://pypi.org/project/mcp/)
[![DuckDB](https://img.shields.io/pypi/v/duckdb?label=duckdb)](https://pypi.org/project/duckdb/)
[![cloudscraper](https://img.shields.io/pypi/v/cloudscraper?label=cloudscraper)](https://pypi.org/project/cloudscraper/)
[![Beautiful Soup](https://img.shields.io/pypi/v/beautifulsoup4?label=beautifulsoup4)](https://pypi.org/project/beautifulsoup4/)
[![lxml](https://img.shields.io/pypi/v/lxml?label=lxml)](https://pypi.org/project/lxml/)
[![tqdm](https://img.shields.io/pypi/v/tqdm?label=tqdm)](https://pypi.org/project/tqdm/)
[![anyio](https://img.shields.io/pypi/v/anyio?label=anyio)](https://pypi.org/project/anyio/)
[![questionary](https://img.shields.io/pypi/v/questionary?label=questionary)](https://pypi.org/project/questionary/)
[![Rich](https://img.shields.io/pypi/v/rich?label=rich)](https://pypi.org/project/rich/)

### Dev & build

[![pytest](https://img.shields.io/pypi/v/pytest?label=pytest)](https://pypi.org/project/pytest/)
[![pytest-asyncio](https://img.shields.io/pypi/v/pytest-asyncio?label=pytest-asyncio)](https://pypi.org/project/pytest-asyncio/)
[![pytest-cov](https://img.shields.io/pypi/v/pytest-cov?label=pytest-cov)](https://pypi.org/project/pytest-cov/)
[![respx](https://img.shields.io/pypi/v/respx?label=respx)](https://pypi.org/project/respx/)
[![Ruff](https://img.shields.io/pypi/v/ruff?label=ruff)](https://pypi.org/project/ruff/)
[![ty](https://img.shields.io/badge/ty-astral--sh-FF4B4B)](https://github.com/astral-sh/ty)
[![Hatchling](https://img.shields.io/pypi/v/hatchling?label=hatchling)](https://pypi.org/project/hatchling/)

### Platform & data sources

[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-workflows-2088FF?logo=githubactions&logoColor=white)](https://github.com/Abstract-Data/texas-result-scraper/actions)
[![GitHub Pages](https://img.shields.io/badge/GitHub_Pages-static_JSON%2FCSV-222222?logo=githubpages&logoColor=white)](docs/adr/003-github-pages-static-api.md)
[![Texas SOS Legacy](https://img.shields.io/badge/data-Texas_SOS_Legacy-002868)](docs/EARLY_VOTING_ROSTER.md)
[![Civix EVR](https://img.shields.io/badge/data-Civix_EVR-1a365d)](docs/CIVIX_EVR_API.md)

## Overview

| Surface | Role |
|--------|------|
| **Library** | Pydantic models and scrapers for legacy SOS and Civix EVR portals |
| **CLI** | `tx-turnout` — fetch rosters, turnout, audits, voterfile matching, and turnout vs roster gap reports |
| **MCP** | FastMCP tools for agents (`list_elections`, `fetch_roster`, `run_audit`, …) |

Scraped data is committed under `data/elections/` and published as a **static API** via GitHub Pages (scheduled refresh — not a live scrape endpoint).

## Install

```bash
uv sync --dev
```

## CLI

```bash
# Civix EVR (2025+)
uv run tx-turnout civix elections              # newest first; ↑/↓ menus in a TTY (questionary)
uv run tx-turnout civix elections --no-interactive
uv run tx-turnout civix turnout 53813 2025-03-01

# Legacy SOS portal (pre-2025)
uv run tx-turnout legacy elections
uv run tx-turnout legacy turnout 49664 2024-10-21
uv run tx-turnout legacy roster 49664 2024-10-21
uv run tx-turnout legacy roster 49664 2024-10-21 --strategy B --out-dir ./data/tmp

# Audit and voterfile match
uv run tx-turnout audit run-inline data/elections/49664/roster_2024-10-21.csv
uv run tx-turnout voterfile match roster.csv /path/to/voterfile.csv

# Turnout vs roster gap report (Civix)
uv run tx-turnout civix gap-report 58315
uv run tx-turnout civix gap-report 58315 --ev-date 2026-05-22 --turnout-source live
```

## Turnout vs roster gap analysis

Civix publishes two related but different numbers during early voting:

| Source | What it counts | Typical use |
|--------|----------------|-------------|
| **Turnout table** | County cumulative ballot totals (in-person + mail) | What SOS posts online (~829k for a runoff) |
| **Roster CSVs** | Named voters in per-county detail files | What `fetch-all` scrapes (~780k unique VUIDs) |

The gap report compares those side by side, **county by county**, so you can see where aggregate turnout and scraped roster detail diverge (often mail ballots counted in turnout before they appear in roster files, especially while an election is uncertified).

### How the report is built

1. **Roster side** — Read the combined Civix roster (`roster_ev_{id}.csv` from `civix fetch-all`). For each county, count unique VUIDs from in-person and mail-only rows across all EV dates in the file.
2. **Turnout side** — Load cumulative county totals for one EV snapshot date:
   - **`auto`** (default): use `turnout_ev_{date}.csv` on disk if present, else fetch live from Civix
   - **`stored`**: use saved turnout CSV only
   - **`live`**: always fetch from the Civix API (matches current online totals)
3. **Gap** — Per county: `turnout_total − roster_total`, with the same split for in-person and mail. Statewide totals are summed across counties.

`civix fetch-all` saves a turnout snapshot for each EV day as `data/elections/civix/{id}/turnout_ev_{date}.csv`, so later gap runs can compare against the turnout that was online **at scrape time** without re-hitting the API.

### Running gap analysis

**Standalone** (Rich table in the terminal + JSON/CSV on disk):

```bash
uv run tx-turnout civix gap-report 58315
uv run tx-turnout civix gap-report 58315 --ev-date 2026-05-22 -o json --no-write-files
```

**With voterfile match** (on by default for Civix `roster_ev_*.csv` paths):

```bash
uv run tx-turnout voterfile match \
  data/elections/civix/58315/roster_ev_58315.csv \
  /path/to/voterfile.csv \
  --no-interactive
```

Use `--no-gap-report` to skip, or `--gap-turnout-source stored|live|auto` to control the turnout source.

### Output files

| File | Description |
|------|-------------|
| `gap_report_ev_{id}.json` | Full report (statewide + all counties) from `civix gap-report` |
| `gap_counties_ev_{id}.csv` | County table for spreadsheets |
| `gap_report_{roster_stem}.json` | Same report embedded when run via `voterfile match` |
| `gap_counties_{roster_stem}.csv` | County CSV from match flow |
| `match_report_*.json` | Includes `turnout_roster_gap` when gap analysis ran |

Implementation: [`src/texas_turnout_scraper/gap_analysis.py`](src/texas_turnout_scraper/gap_analysis.py); terminal formatting: [`src/texas_turnout_scraper/terminal_report.py`](src/texas_turnout_scraper/terminal_report.py).

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
- [`docs/CIVIX_EVR_API.md`](docs/CIVIX_EVR_API.md) — Civix EVR API reference

## Data

Cached election data lives under `data/elections/` and is refreshed daily by the [Data Refresh](.github/workflows/data-refresh.yml) workflow. Consumers pull from GitHub Pages static JSON/CSV — not a real-time scrape endpoint.

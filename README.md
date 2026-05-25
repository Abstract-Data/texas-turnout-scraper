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
| **CLI** | `tx-turnout` — fetch rosters, turnout, audits, and voterfile matching |
| **MCP** | FastMCP tools for agents (`list_elections`, `fetch_roster`, `run_audit`, …) |

Scraped data is committed under `data/elections/` and published as a **static API** via GitHub Pages (scheduled refresh — not a live scrape endpoint).

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
- [`docs/CIVIX_EVR_API.md`](docs/CIVIX_EVR_API.md) — Civix EVR API reference

## Data

Cached election data lives under `data/elections/` and is refreshed daily by the [Data Refresh](.github/workflows/data-refresh.yml) workflow. Consumers pull from GitHub Pages static JSON/CSV — not a real-time scrape endpoint.

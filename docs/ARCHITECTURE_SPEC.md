# Texas Turnout Scraper — Architecture Spec

**Status:** Draft — Pre-implementation  
**Date:** 2026-05-24  
**Author:** John Eakin

---

## Overview

`texas-turnout-scraper` is a Python package that pulls early voting turnout data from the Texas
Secretary of State's election portals and returns structured, typed outputs consumers can plug
into their own databases or pipelines.

The package ships three interfaces over the same core library:

1. **Core library** — async Python, `httpx` + Pydantic. No Selenium, no FastAPI, no ORM.
2. **CLI** — `Typer`-based, for humans and agents running in a shell.
3. **MCP server** — wraps the core library as callable tools for AI agents.
4. **Skill** — documents the workflow for agents using the MCP.

### Two Data Sources

| Source | Coverage | Base URL | Auth |
|--------|----------|----------|------|
| **Civix EVR** (primary) | 2025+ (current elections) | `goelect.txelections.civixapps.com` | None — stateless REST |
| **Legacy SOS** (historical) | Pre-2025 elections | `earlyvoting.texas-election.com` | JSESSIONID session |

The scraper is **not real-time** — it runs on a schedule and commits data to `data/`. Consumers
always pull from the cached GitHub Pages static API. There is no `--source live` mode.

Full Civix API documentation: [`docs/CIVIX_EVR_API.md`](./CIVIX_EVR_API.md)  
Full legacy SOS documentation: [`docs/EARLY_VOTING_ROSTER.md`](./EARLY_VOTING_ROSTER.md)

---

## What We're Replacing

The original implementation used Selenium to drive a Chrome browser through the SOS portal.
This is being fully replaced with direct HTTP session management (`httpx`) based on the
reverse-engineered request flow documented in `docs/EARLY_VOTING_ROSTER.md`.

**Removed entirely:**
- Selenium / ChromeDriver dependency
- `election_utils` local package dependency
- All existing data models (`EarlyVoteDayData`, `ElectionSelector`, `ReadElectionData`, etc.)
- Interactive `input()` prompts
- Hard-coded CSS selectors (e.g. `tr:nth-child(255)`)
- `sleep()` polling loops for download completion

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                           Core Library                                │
│                                                                        │
│  ── Civix source (2025+) ──────────────────────────────────────────  │
│  civix.py         — stateless REST client, base64 envelope decode     │
│                                                                        │
│  ── Legacy SOS source (pre-2025) ──────────────────────────────────  │
│  session.py       — JSESSIONID management, POST sequence              │
│  elections.py     — election discovery from HTML select               │
│  roster.py        — voter roster fetch (Strategy A/B)                 │
│  turnout.py       — county turnout HTML table parsing                 │
│                                                                        │
│  ── Shared ────────────────────────────────────────────────────────  │
│  audit.py         — post-processing data quality checks               │
│  models.py        — all Pydantic models (shared across sources)       │
│  enums.py         — ElectionType, VoteMethod, Party                   │
└───────────────────────────┬──────────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
   ┌──────────▼────────┐        ┌──────────▼────────┐
   │   CLI (Typer)     │        │   MCP Server      │
   │  tx-turnout       │        │  FastMCP tools    │
   │    civix …        │        │  for AI agents    │
   │    legacy …       │        └───────────────────┘
   │    audit …        │
   └───────────────────┘
              │
   ┌──────────▼────────────────────┐
   │  data/                         │
   │  ├── elections/civix/{id}/     │  ← Civix (2025+)
   │  └── elections/legacy/{id}/    │  ← Legacy SOS (pre-2025)
   └────────────────────────────────┘
              │
   GitHub Pages static API (consumers pull via HTTP GET)
```

---

## HTTP Request Flows

### Civix EVR (2025+ elections) — Stateless REST

Per `docs/CIVIX_EVR_API.md`. All requests are plain `GET`. No session, no cookies.

```
GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTION
    → Election index (all elections + EV dates + county list)

GET /api-ivis-system/api/v1/getFile?type=EVR_EARLYVOTING&electionId={id}&electionDate={date}
    → County turnout table (JSON, base64-wrapped)

GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_EARLYVOTING&...&county={name}&countyId={id}&format=csv
    → Per-county voter roster CSV (base64-wrapped)

GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTIONDAYTURNOUT&electionId={id}&electionDate={date}
    → Election day county turnout (JSON, base64-wrapped)

GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_ELECTIONDAYTURNOUT&...&format=zip
    → Election day per-county roster ZIP (base64-wrapped)

GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_COUNTYPLACEINFO&electionId={id}&name=STATEWIDE_POLLING_PLACE_INFO&format=csv
    → Statewide polling place info CSV (base64-wrapped)
```

**All responses:** `{"upload": "<base64>"}` — decode before parsing.  
**Pacing:** ≥1.0 s between county roster requests (~255 per election date).

### Legacy SOS (pre-2025 elections) — Stateful Session

Per `docs/EARLY_VOTING_ROSTER.md`. Requires `JSESSIONID` cookie session.

```
GET  getElectionDetails.do              → HTML election picker
POST getElectionEVDates.do             → body: idElection={id}
POST getEVDetails.do                   → body: idElection + selectedDate
POST downloadVoterInfoReport.do        → body: above + idTown={townId}  (Strategy A)
POST downloadParticipationCountReport.do → body: above + idTown=        (Strategy B)
```

**Pacing:** ≥1.0 s between requests.

---

## Fetch Strategies (applies to both sources)

Default: **Strategy A** (per-county loop).

| | Strategy A — per-county loop | Strategy B — bulk |
|---|---|---|
| Civix endpoint | `getFileByFormat?format=csv` ×254 | `getFile?type=EVR_STATEWIDE` ×1 (may 502) |
| Legacy endpoint | `downloadVoterInfoReport.do` ×254 | `downloadParticipationCountReport.do` ×1 |
| Requests | ~255 per date | 1 per date |
| `VOTING_METHOD` split | ✅ `IN-PERSON` / `MAIL-IN` | ❌ Not available |
| Payload | Many small CSVs | One large file/ZIP |
| Reliability | ✅ Works for all elections | ⚠️ Civix 502s on large elections |

Strategy A is the default and preferred. Strategy B is available via `--strategy B` for
fast statewide snapshots when the method split is not needed.

---

## Data Models (`models.py`)

All models are **pure Pydantic** — no SQLAlchemy, no SQLModel, no ORM coupling.
Consumers serialize to whatever persistence layer they choose.

### Enums (`enums.py`)

```python
class ElectionType(str, Enum):
    PRIMARY = "primary"
    PRIMARY_RUNOFF = "primary_runoff"
    GENERAL = "general"
    SPECIAL = "special"
    CONSTITUTIONAL_AMENDMENT = "constitutional_amendment"
    LOCAL = "local"
    UNKNOWN = "unknown"

class VoteMethod(str, Enum):
    IN_PERSON = "IN-PERSON"
    MAIL_IN = "MAIL-IN"
    ELECTION_DAY = "GE"           # observed in bulk file only

class PoliticalParty(str, Enum):
    REPUBLICAN = "republican"
    DEMOCRATIC = "democratic"
    NONPARTISAN = "nonpartisan"   # general elections
```

Election type is inferred from the SOS option label text:
- `PRIMARY RUNOFF` → `primary_runoff`
- `PRIMARY` → `primary`
- `GENERAL` → `general`
- `SPECIAL` → `special`
- `CONSTITUTIONAL` → `constitutional_amendment`
- `LOCAL` → `local`
- else → `unknown`

### Core Models

```python
class Election(BaseModel):
    source_election_id: str          # SOS numeric ID — canonical unique key (e.g. "49664")
    label: str                       # generated slug for display/filenames (e.g. "TX-2024-GE")
    name: str                        # raw SOS option text (e.g. "2024 NOVEMBER 5TH GENERAL ELECTION")
    election_type: ElectionType
    party: Optional[PoliticalParty]  # None for GENERAL; set for PRIMARY elections
    state: str = "TX"
    election_date: date              # parsed from name


class CountyTurnout(BaseModel):
    election_id: str                 # references Election.source_election_id
    report_date: date                # the selectedDate this row was fetched for
    county: str                      # "STATEWIDE" or county name
    registered_voters: int
    inperson_on_date: int            # in-person voters on report_date only
    cumulative_inperson: int         # running total through report_date
    cumulative_inperson_pct: float
    cumulative_mail: int             # running total through report_date
    cumulative_total: int            # inperson + mail
    cumulative_total_pct: float


class VoterRecord(BaseModel):
    election_id: str                 # references Election.source_election_id
    report_date: date                # the selectedDate this record was fetched for
    voter_id: str                    # Texas VUID — always a string, never coerced to int (PII)
    voter_name: str                  # full name as published by SOS (PII — consumer's responsibility)
    vote_method: VoteMethod
    county: Optional[str]            # None when fetched from statewide bulk file
    precinct: str
    poll_place_id: Optional[str]     # present in statewide file only
    poll_place_name: Optional[str]   # present in statewide file only


class ElectionRoster(BaseModel):
    """Full result of a roster fetch for one election + date."""
    election: Election
    report_date: date
    strategy: Literal["A", "B"]
    counties_fetched: int
    total_records: int
    records: List[VoterRecord]


class AuditReport(BaseModel):
    """Post-processing data quality report. Run separately on fetched data."""
    election_id: str
    report_date: date
    total_records: int
    duplicate_vuids: int             # same voter_id appears more than once
    duplicate_vuid_ids: List[str]    # the actual duplicate VUIDs
    cross_method_duplicates: int     # VUID appears with both IN-PERSON and MAIL-IN
    cross_method_vuid_ids: List[str]
    turnout_exceeds_registered: List[str]  # county names where total > registered voters
    missing_counties: List[str]      # counties expected but absent from fetch
    notes: List[str]                 # human-readable summary of findings
```

---

## CLI (`cli/` package — Typer)

Binary name: `tx-turnout`

```
# ── Civix source (2025+ current elections) ──────────────────────────
tx-turnout civix elections list                    # list elections from Civix EVR index
tx-turnout civix elections describe <id>           # full details for one Civix election

tx-turnout civix roster fetch <id>                 # fetch voter roster (EV or Election Day)
    --date <YYYY-MM-DD>                            # EV date; omit for election day
    --county <NAME>                                # single county; omit for all
    --strategy [A|B]                               # default A
    --output [json|csv|ndjson]
    --out-file <path>

tx-turnout civix turnout fetch <id>                # fetch county EV turnout table
    --date <YYYY-MM-DD>
    --output [json|csv]
    --out-file <path>

tx-turnout civix polling-places fetch <id>         # fetch polling place info CSV
    --county <NAME|STATEWIDE>                      # default STATEWIDE
    --out-file <path>

# ── Legacy SOS source (pre-2025 historical) ─────────────────────────
tx-turnout legacy elections list
tx-turnout legacy elections describe <id>
tx-turnout legacy roster fetch <id>
    --date <YYYY-MM-DD>
    --strategy [A|B]
    --output [json|csv|ndjson]
    --out-file <path>
tx-turnout legacy turnout fetch <id>
    --date <YYYY-MM-DD>

# ── Shared audit commands ────────────────────────────────────────────
tx-turnout audit run <file>                        # audit a fetched roster file
    --output [json|text]
    --out-file <path>

tx-turnout audit run-inline <id>                   # fetch + audit in one command
    --source [civix|legacy]                        # required
    --date <YYYY-MM-DD>
    --strategy [A|B]
```

All commands support `--help`. Output defaults to stdout so commands compose with pipes.

---

## MCP Server (`mcp_server.py`)

Exposed tools mirror the CLI commands. All tools return serialized Pydantic models (JSON).

### Civix Tools (2025+ elections)

| Tool | Description |
|------|-------------|
| `civix_list_elections` | Returns all elections from Civix EVR index |
| `civix_describe_election` | Full details for one Civix election by source_election_id |
| `civix_fetch_turnout` | EV county turnout table for one election + date |
| `civix_fetch_roster` | Per-county voter roster CSV for one election + date |
| `civix_fetch_polling_places` | Polling place info for one election (statewide or per-county) |
| `civix_fetch_election_day_turnout` | Election day county turnout table |

### Legacy SOS Tools (pre-2025 elections)

| Tool | Description |
|------|-------------|
| `legacy_list_elections` | Returns all elections from legacy SOS portal |
| `legacy_describe_election` | Full details for one legacy election |
| `legacy_fetch_roster` | Voter roster for one election + date (Strategy A or B) |
| `legacy_fetch_turnout` | County turnout table for one election + date |

### Shared Tools

| Tool | Description |
|------|-------------|
| `run_audit` | Runs AuditReport on a provided roster file path or ElectionRoster |

---

## Skill

The Skill documents the workflow for agents using the MCP:

1. When to use Strategy A vs B
2. How to interpret an AuditReport (what counts as a meaningful anomaly)
3. PII handling — VOTER_NAME and voter_id are public record under the Texas Election Code
   but should not be logged or cached in API responses unless a downstream feature requires it
4. How election_type is inferred and when UNKNOWN should be escalated
5. Pacing requirements for Strategy A (~255 requests, ≥1.0s between)

---

## PII Handling

`VOTER_NAME` and `voter_id` are personal data. Early-voting rosters are public record under
the Texas Election Code. The package includes them in `VoterRecord` because consumers need
them for deduplication and database joins.

**Package responsibilities:**
- Never log row contents (names, VUIDs)
- Never cache raw roster data in MCP responses unless explicitly requested
- Document PII fields clearly in models

**Consumer responsibilities:**
- Restrict access to stored roster tables
- Exclude raw names from any public-facing API responses

---

## Data Storage in Repo (Flat Viewer)

Election data is committed directly to the repository so that anyone who clones it has
the latest data immediately, and so files can be viewed interactively via the
[Flat Viewer](https://flat.githubocto.com).

### Important: Flat Action vs Flat Viewer

The `githubocto/flat` GitHub Action only supports `http` (simple GET) and `sql` fetch modes.
Our SOS source requires a stateful POST session — it cannot be a simple GET. Therefore:

- We do **not** use the Flat *action* as the data fetcher.
- We use a standard GitHub Actions workflow that runs `tx-turnout` CLI to produce files.
- Those files are committed to `data/` via standard `git commit`.
- The **Flat Viewer** (`flat.githubocto.com`) can view any CSV/JSON committed to a GitHub
  repo regardless of how they were committed — it is a viewer, not a required commit mechanism.

### Data Directory Layout

```
data/
├── index.json                               # master index: both sources, all elections
├── elections/
│   ├── civix/                               # Civix source (2025+)
│   │   ├── index.json                       # Civix election list
│   │   └── {source_election_id}/
│   │       ├── election.json                # Election model
│   │       ├── turnout_ev_{YYYY-MM-DD}.csv  # EV county turnout per date
│   │       ├── turnout_ed_{YYYY-MM-DD}.csv  # Election day county turnout
│   │       ├── roster_ev_{YYYY-MM-DD}.csv   # EV voter roster per date
│   │       ├── roster_ed_{YYYY-MM-DD}.csv   # Election day voter roster
│   │       ├── polling_places.csv           # Polling place info (one per election)
│   │       └── audit_{YYYY-MM-DD}.json      # AuditReport per date
│   └── legacy/                              # Legacy SOS source (pre-2025)
│       ├── index.json                       # Legacy election list
│       └── {source_election_id}/
│           ├── election.json
│           ├── roster_{YYYY-MM-DD}.csv
│           ├── turnout_{YYYY-MM-DD}.csv
│           └── audit_{YYYY-MM-DD}.json
```

Files are append-only — new dates are added, existing files are not overwritten.
`data/index.json` is the single entry point for consumers: lists all elections from both
sources with their source prefix, IDs, and available file dates.

### GitHub Action: Data Refresh

`.github/workflows/data-refresh.yml`

```yaml
name: Refresh Election Data

on:
  schedule:
    - cron: '0 8 * * *'       # daily at 8 AM CT during election season
  workflow_dispatch:           # manual trigger

jobs:
  fetch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install package
        run: pip install .

      - name: Fetch latest data
        run: |
          tx-turnout elections list --output json --out-file data/elections/index.json
          # Fetch roster + audit for each active election (driven by index.json)
          tx-turnout roster fetch-all \
            --index data/elections/index.json \
            --output-dir data/elections \
            --strategy A

      - name: Commit if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/
          git diff --staged --quiet || git commit -m "data: refresh $(date -u +%Y-%m-%d)"
          git push
```

### GitHub Pages Static API

Enabling GitHub Pages on the repo turns every committed data file into a public HTTP endpoint:

```
https://{org}.github.io/texas-turnout-scraper/data/elections/index.json
https://{org}.github.io/texas-turnout-scraper/data/elections/49664/election.json
https://{org}.github.io/texas-turnout-scraper/data/elections/49664/roster_2024-10-21.csv
https://{org}.github.io/texas-turnout-scraper/data/elections/49664/audit_2024-10-21.json
```

This separates **producing** data (the scraper, runs on a schedule) from **consuming** data
(anyone, via plain HTTP GET, no session or scraper required).

### Consumer Model

The scraper is **not real-time**. There is no `--source live` mode for consumers.

- The CLI/MCP **fetch** commands (`civix roster fetch`, `legacy roster fetch`) are run by the
  scheduled GitHub Actions workflow, not by end consumers.
- **Consumers** (downstream scripts, AI agents, dashboards) pull from the GitHub Pages static
  API via plain HTTP GET — no scraper, no session, no SOS load.
- The `data/index.json` file tells consumers what elections and dates are available.

### Flat Viewer Links

Point the Flat Viewer at any CSV file via the GitHub Pages URL:

```
https://flat.githubocto.com/?url=https://{org}.github.io/texas-turnout-scraper/data/elections/{id}/roster_{date}.csv
```

The `index.json` file serves as the entry point for downstream consumers (agents, scripts, other
pipelines) to discover what data is available without having to list directory contents.

---

## Package Structure (Target)

```
texas-turnout-scraper/
├── src/
│   └── texas_turnout_scraper/
│       ├── __init__.py
│       ├── enums.py              # ElectionType, VoteMethod, PoliticalParty
│       ├── models.py             # all Pydantic models (shared across sources)
│       ├── civix.py              # Civix EVR API client (stateless REST, base64 decode)
│       ├── session.py            # Legacy: JSESSIONID management
│       ├── elections.py          # Legacy: election discovery from HTML
│       ├── roster.py             # Legacy: voter roster fetch (Strategy A + B)
│       ├── turnout.py            # Legacy: county turnout HTML table parsing
│       ├── audit.py              # AuditReport post-processing (shared)
│       ├── cli/                  # Typer CLI package (civix, legacy, audit, voterfile)
│       │   ├── __init__.py       # Exports `app`; side-effect imports register commands
│       │   ├── _typer_apps.py    # Root Typer apps (civix, legacy, audit, voterfile)
│       │   ├── _common.py        # Shared index/freshness helpers
│       │   ├── civix.py          # Civix subcommands
│       │   ├── legacy.py         # Legacy subcommands
│       │   ├── audit.py          # Audit subcommands
│       │   └── voterfile.py      # Voterfile match commands
│       └── mcp_server.py         # MCP server (civix + legacy + audit tools)
├── tests/
│   ├── __init__.py
│   ├── unit/
│   │   ├── test_civix.py         # Civix API parsing (mocked httpx with respx)
│   │   ├── test_session.py
│   │   ├── test_elections.py
│   │   ├── test_roster.py
│   │   ├── test_turnout.py
│   │   ├── test_audit.py
│   │   └── test_models.py
│   ├── integration/
│   │   ├── test_civix_live.py    # @pytest.mark.integration — hits live Civix API
│   │   └── test_legacy_live.py   # @pytest.mark.integration — hits live SOS site
│   ├── fixtures/
│   │   └── early_voting/
│   │       ├── civix_election_index.json   # EVR_ELECTION response (decoded)
│   │       ├── civix_earlyvoting_53813.json
│   │       ├── civix_roster_harris_sample.csv  # synthesized PII, 10 rows
│   │       ├── legacy_election_index.html
│   │       ├── legacy_getEVDetails_49664.html
│   │       └── legacy_voter_info_loving.csv    # Loving County, 6 rows
│   └── verify/
│       └── check_agents_md.py
├── docs/
│   ├── CIVIX_EVR_API.md          # Civix API reference (endpoints, schemas, observed data)
│   ├── EARLY_VOTING_ROSTER.md    # Legacy SOS HTTP flow reference
│   ├── ARCHITECTURE_SPEC.md      # this file
│   ├── ARCHITECTURE.md
│   ├── TESTING.md
│   ├── GUARDRAILS.md
│   └── adr/
├── pyproject.toml
├── AGENTS.md
└── README.md
```

---

## Dependencies (Target)

```toml
[project]
requires-python = ">=3.10.5,<3.13"
dependencies = [
    "httpx>=0.27",       # async HTTP client (replaces Selenium)
    "pydantic>=2.9",     # data models
    "typer>=0.12",  # CLI
    "mcp[cli]>=1.0",     # MCP server SDK
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
    "ty>=0.0.39",
]
```

**Removed:** `selenium`, `election_utils`, `icecream`, `logfire`, `tqdm`

---

## Key Design Decisions

1. **No FastAPI** — The package is a library + CLI + MCP. FastAPI can be added later if a
   hosted service becomes necessary (e.g. for `txelections.live`), but it is not needed for
   the current use cases.

2. **Pure Pydantic, no SQLModel** — Models are database-agnostic. Consumers bring their own
   persistence layer. SQLModel would couple the package to SQLAlchemy unnecessarily.

3. **source_election_id as canonical key** — The SOS numeric ID (e.g. `49664`) is the stable,
   collision-free identifier. The human-readable `label` (e.g. `TX-2024-GE`) is a display field.

4. **Audit as post-processing** — `AuditReport` runs on already-fetched data. This keeps the
   fetch logic clean and allows re-auditing historical data without re-scraping.

5. **VOTER_NAME always included** — PII responsibility lies with the consumer. The package
   documents it clearly and never logs it internally.

6. **Strategy A as default** — The in-person/mail split (`VOTING_METHOD`) is a core output.
   Strategy B (bulk ZIP) drops this column; use only when speed matters more than method detail.

7. **election_utils removed** — All models and enums are defined fresh in this package.
   No inherited constraints from the old shared library.

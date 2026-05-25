# Architecture — texas-turnout-scraper

> For the full design spec including Pydantic model schemas, CLI commands, MCP tools, and
> GitHub Pages static API layout, see [`ARCHITECTURE_SPEC.md`](./ARCHITECTURE_SPEC.md).
> For the SOS HTTP flow reference (endpoints, request/response schemas, session handling),
> see [`EARLY_VOTING_ROSTER.md`](./EARLY_VOTING_ROSTER.md).

---

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                  texas-turnout-scraper                       │
│                                                              │
│  ┌──────────┐   ┌───────────┐   ┌──────────────────────┐   │
│  │ session  │→  │ elections │   │ roster / turnout /   │   │
│  │ .py      │   │ .py       │   │ audit modules        │   │
│  │ (httpx)  │   │           │   │                      │   │
│  └──────────┘   └───────────┘   └──────────────────────┘   │
│        ↑                ↑                   ↑               │
│        └────────────────┴───────────────────┘               │
│                    Core Library                              │
│                         ↓                                   │
│          ┌──────────────┼──────────────┐                    │
│          ↓              ↓              ↓                    │
│      cli.py       mcp_server.py    data/ files              │
│    (Typer)        (FastMCP)       (CSV/JSON)                 │
│    tx-turnout                                                │
└─────────────────────────────────────────────────────────────┘
         ↓                  ↓                   ↓
    Human users        AI agents          GitHub Pages
                                       static JSON/CSV API
```

## Key Design Decisions

**httpx replaces Selenium.** The SOS site is a stateful Java/Struts form application. Its
session can be established entirely with HTTP requests — no browser needed. A `JSESSIONID`
cookie is obtained by walking the form flow: `GET getElectionDetails.do` →
`POST getElectionEVDates.do` → `POST getEVDetails.do`.

**Pydantic v2, no SQLModel.** All output models are database-agnostic Pydantic. Consumers
own their own persistence layer.

**Strategy A (per-county) is default.** ~255 requests per election date, paced at ≥1.0 s,
preserves the `VOTING_METHOD` (IN-PERSON / MAIL-IN) split. Strategy B (bulk ZIP) is
available for fast full-state snapshots without the method split.

**No real-time mode.** The scraper runs on a schedule. Consumers pull from GitHub Pages
cached data via HTTP GET. The `data/` directory is committed to the repo and served via
GitHub Pages.

**`source_election_id` is always a string.** SOS numeric IDs (e.g. `"49664"`) are the
canonical key. Never coerce to int. Same for `ID_VOTER` (Texas VUID).

## Data Flow

```
GitHub Actions (daily schedule)
    └─→ tx-turnout elections list        ← getElectionDetails.do
    └─→ tx-turnout roster fetch {id}     ← per-county POST loop (Strategy A)
    └─→ tx-turnout turnout fetch {id}    ← getEVDetails.do
    └─→ tx-turnout audit run {id}        ← post-processing on roster CSV
    └─→ git commit data/ → push → GitHub Pages
                                    ↓
                         consumers: curl https://{org}.github.io/
                                    texas-turnout-scraper/data/
                                    elections/index.json
```

## Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `session.py` | Establish httpx session, manage `JSESSIONID` cookie |
| `elections.py` | Parse election picker, return `List[Election]` |
| `roster.py` | Scrape per-county voter roster CSV (Strategy A) |
| `turnout.py` | Scrape county turnout HTML table, return `List[CountyTurnout]` |
| `audit.py` | Post-process roster: detect duplicate VUIDs, cross-method duplicates, anomalies |
| `models.py` | All Pydantic output models |
| `enums.py` | `ElectionType`, `VoteMethod`, `PoliticalParty` enums |
| `cli.py` | Typer CLI (`tx-turnout`) |
| `mcp_server.py` | FastMCP server exposing 5 tools to AI agents |

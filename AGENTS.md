# AGENTS.md
# Version: 1.3.0
# Last Updated: 2026-05-25
# Environment: dev
# Model: claude-sonnet-4-6
# Fallback Model: claude-haiku-4-5-20251001
# Project: texas-turnout-scraper
# Maintainer: John Eakin

---

**Project Name:** texas-turnout-scraper
**Project Type:** Python package (library + CLI + MCP server)
**Stack:** Python 3.10–3.12 · httpx · Pydantic v2 · Typer · FastMCP · DuckDB
**Binary:** `tx-turnout`

---

## Agent Scope

```
Reads:      src/, tests/, docs/, data/, prompts/, .claude/, .cursor/, pyproject.toml, AGENTS.md, HANDOFF.md
Writes:     src/, tests/, docs/, data/, prompts/, HANDOFF.md, .claude/handoffs/
Executes:   uv, ruff, pytest, ty, git (feature branches only), gh (read + PR creation)
Off-limits: .env, prod database, /secrets/, any other repository, earlyvoting.texas-election.com (without established JSESSIONID session)
```

---

## Project Purpose

`texas-turnout-scraper` is a Python library and CLI that scrapes early-voting turnout data
and voter rosters from the Texas Secretary of State's stateful Java/Struts portal
(`earlyvoting.texas-election.com`). It produces structured Pydantic output that consumers
can plug into any database. It also exposes an MCP server so AI agents can query elections
and rosters directly.

Data is committed to the `data/` directory on a schedule and served as a static JSON/CSV
API via GitHub Pages. **The scraper is not real-time** — consumers always pull from cached
GitHub Pages data; the scheduled workflow keeps it fresh.

Full architecture is in [`docs/ARCHITECTURE_SPEC.md`](docs/ARCHITECTURE_SPEC.md).
HTTP flow documentation is in [`docs/EARLY_VOTING_ROSTER.md`](docs/EARLY_VOTING_ROSTER.md).

---

## Repository Layout

```
texas-turnout-scraper/
├── src/texas_turnout_scraper/
│   ├── __init__.py
│   ├── enums.py          # ElectionType, VoteMethod, PoliticalParty
│   ├── models.py         # Pydantic models: VoterRecord, CountyRoster, ColumnMapping, …
│   ├── session.py        # LegacySession + JSESSIONID + prime_election
│   ├── legacy_api.py     # Session-managed facades for CLI/MCP
│   ├── elections.py      # Election discovery and metadata
│   ├── roster.py         # Per-county voter roster scraping (Strategy A)
│   ├── turnout.py        # County turnout table scraping
│   ├── audit.py          # Data quality audit: duplicate VUIDs, anomaly detection
│   ├── writer.py         # accumulate_roster, write_roster_csv, audit helpers
│   ├── voterfile.py      # DuckDB-based voterfile match engine + column detection
│   ├── cli.py            # Typer CLI entry point (`tx-turnout`)
│   └── mcp_server.py     # FastMCP server exposing core tools to AI agents
├── data/
│   └── elections/
│       ├── index.json               # Election listing
│       └── {source_election_id}/
│           ├── election.json
│           ├── roster_{date}.csv
│           ├── turnout_{date}.csv
│           └── audit_{date}.json
├── docs/
│   ├── ARCHITECTURE_SPEC.md   # Full refactor spec (source of truth)
│   ├── EARLY_VOTING_ROSTER.md # SOS HTTP flow documentation
│   ├── ARCHITECTURE.md
│   ├── TESTING.md
│   └── GUARDRAILS.md
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   │   ├── early_voting/
│   │   └── voterfiles/          # sample_voterfile.csv (synthetic)
│   └── verify/check_agents_md.py
├── .claude/
│   ├── agents/
│   ├── handoffs/                # gitignored — archived session handoffs
│   ├── hooks/
│   ├── settings.json
│   └── skills/
├── .cursor/rules/
├── .github/workflows/
│   ├── ci.yml
│   ├── data-refresh.yml   # Daily scheduled data pull + commit
│   └── tool-config-verify.yml
├── prompts/
│   ├── README.md              # Version registry (one section per prompt)
│   └── {prompt-name}/
│       ├── current.md         # Active version (starts with # Version: X.Y.Z)
│       ├── v{X.Y.Z}.md        # Versioned snapshot (required — not stub)
│       └── .gitkeep
├── docs/adr/
├── pyproject.toml
├── AGENTS.md              ← this file (versioned — bump on behavior change)
└── CLAUDE.md              → symlink to AGENTS.md
```

---

## Model Configuration

```
Primary:  claude-sonnet-4-6
Fallback: claude-haiku-4-5-20251001
Notes:    Use Sonnet for implementation and complex refactors.
          Use Haiku for quick lookups, fixture generation, and formatting tasks.
          DuckDB queries run against local files — no model context needed for data scans.
```

---

## Core Commands

```bash
# CLI
tx-turnout elections list                    # List all known elections
tx-turnout elections describe 49664          # Describe one election
tx-turnout roster fetch 49664 --date 2024-10-21   # Fetch county rosters (Strategy A)
tx-turnout turnout fetch 49664 --date 2024-10-21  # Fetch county turnout table
tx-turnout audit run 49664 --date 2024-10-21      # Run data quality audit
tx-turnout audit run-inline data/elections/49664/roster_2024-10-21.csv
tx-turnout voterfile detect-columns /path/to/voterfile.csv
tx-turnout voterfile match roster_ev_53813.csv /path/to/voterfile.csv

# Dev
uv sync --dev
ruff check . --fix && ruff format .
ty check
pytest tests/unit -q
pytest tests/integration -q   # hits live SOS site; needs network
```

---

## Key Constraints

- **httpx + cloudscraper (no Selenium)** — Default HTTP backend is `cloudscraper` for WAF bypass; unit tests use `http_backend="httpx"`. Legacy session: `GET getElectionDetails.do` → `POST getElectionEVDates.do` → `POST getEVDetails.do`, carrying `JSESSIONID` forward.
- **No `election_utils` dependency** — all models are Pydantic v2, defined in this package. ✅ Use `from texas_turnout_scraper.models import ...`
- **No SQLModel / SQLAlchemy** — output models are database-agnostic Pydantic. ✅ Use DuckDB for large-file queries; Pydantic for all output models.
- **Strategy A is default** — per-county roster loop (~255 requests per date, ≥1.0 s pacing). Strategy B (bulk ZIP) available via `--strategy B` flag.
- **`source_election_id` is the canonical key** — always a string (SOS numeric ID e.g. `"49664"`). ✅ Keep as `str`; validate at parse time with `str(raw_id)`.
- **`ID_VOTER` is always a string** — 10-digit Texas VUID, may have leading zeros. ✅ Use `.zfill(10)` when normalising; store as `str`.
- **PII** — `VOTER_NAME` and `ID_VOTER` are public record under Texas Election Code but ingest must not log row contents. ✅ Write to CSV output; never log or include in exception messages.
- **`.gitignore` has `!data/**/*.csv` exception** — the `*.csv` rule must not block data files.
- **Data lives in `data/` committed to the repo** — GitHub Pages serves it as a static API.
- **No real-time endpoint** — the scraper is scheduled; consumers pull from GitHub Pages cache.

---

## Pacing & Volume

Strategy A issues ~255 requests per election date. Pace at **≥1.0 s** between requests to match the legacy ingest convention. Strategy B is a single large download (~35 MB ZIP); stream to disk, do not buffer in memory.

---

## Data Quality Audit

The `audit` module runs as a separate post-processing step (not inline with fetch). It checks:

- Duplicate VUIDs within a roster
- VUIDs appearing with both IN-PERSON and MAIL-IN methods (cross-method duplicates)
- County turnout exceeding registered voter count
- Missing counties (counties present in the turnout table but absent from the roster)

Results are written to `data/elections/{id}/audit_{date}.json` and returned as `AuditReport`.

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `list_elections` | Return all elections from `data/elections/index.json` |
| `describe_election` | Return metadata for one election by `source_election_id` |
| `fetch_roster` | Scrape per-county voter roster for an election + date |
| `fetch_turnout` | Scrape county turnout table for an election + date |
| `run_audit` | Run data quality audit on a stored roster |

---

## Documentation Priority

When looking for context, read in this order:

1. **Context7 MCP** — for any third-party library API (httpx, Pydantic v2, Typer, FastMCP, DuckDB, BeautifulSoup, cloudscraper, respx, ty, ruff). Use `resolve-library-id` then `get-library-docs` before quoting any library API. **Never quote APIs from memory.**
2. `docs/ARCHITECTURE_SPEC.md` — full design decisions and model schemas
3. `docs/EARLY_VOTING_ROSTER.md` — SOS HTTP flow reference (endpoints, schemas, session)
4. `docs/CIVIX_EVR_API.md` — Civix EVR API reference (envelope schema, county IDs)
5. `docs/GUARDRAILS.md` — what not to do
6. `docs/TESTING.md` — test strategy
7. `docs/RUNBOOK.md` — operational runbook (cloudscraper notes, pacing)
8. `docs/GITBUTLER.md` — virtual branch reference (this project uses GitButler)
9. `docs/playbooks/` — project-specific playbooks (dual-source pattern, MCP testing)
10. `docs/adr/` — decision history; consult before re-litigating a settled design choice
11. This file (`AGENTS.md`)

---

## Goal Proposal Protocol

Before implementing any significant change:

1. State the goal in one sentence
2. List the files you will touch
3. List the files you will NOT touch
4. Identify the biggest risk
5. Get confirmation before proceeding

---

## Session Management

- Read `HANDOFF.md` at session start if it exists — it contains the previous session's state, decisions, blockers, and the single specific next action.
- Write `HANDOFF.md` before clearing context, ending a session, or handing off to another agent. Include: what was completed, what is in progress, blockers, and next suggested action.
- Archive consumed handoffs to `.claude/handoffs/{YYYY-MM-DD}-{slug}.md` after the next session picks them up.
- `.claude/handoffs/` is gitignored — it contains internal session state and partial reasoning.

---

## Notion References

- **Tasks DB:** `collection://2e97d7f5-6298-80a5-acef-000bb9796a9d`
- **Project Page:** https://www.notion.so/36c7d7f5629881a0841df6b1da456fca (`texas-turnout-scraper`)
- **Client Page:** https://www.notion.so/2f37d7f5629881bb814de76479af10db (Abstract Data Internal)
- **Docs DB (for review reports, ADR exports):** `collection://2e97d7f5-6298-804c-b8a5-000b18b72684`
- **Skill Run Log:** `collection://d22fe5bc-922a-4872-9859-99318bf98b61`
- **Dev Environment:** [DEV-ENV-INDEX](https://www.notion.so/3617d7f56298814899f2d14b8f1e5145)
- **AGENTS.md Template:** [AGENTS.md (Base) v1.2.0](https://www.notion.so/2ee7d7f5629880fea6f0e412b3ac6a64)
- **Prompt Library:** `prompts/README.md` (local version registry)

---

## NEVER DO

Each prohibition is paired with the correct alternative:

- 🚫 Never fetch `earlyvoting.texas-election.com` without an established `JSESSIONID` session — ✅ always call `establish_session()` first and carry the cookie forward
- 🚫 Never coerce `source_election_id` or `ID_VOTER` to int — ✅ store as `str`; normalise VUIDs with `.zfill(10)`
- 🚫 Never use Selenium — ✅ httpx only; session establishment is three sequential HTTP calls with cookie forwarding
- 🚫 Never log voter name (`VOTER_NAME`) or VUID (`ID_VOTER`) values — ✅ write them to CSV output (public record); use record counts in log messages
- 🚫 Never import from `election_utils` — ✅ all models are in `texas_turnout_scraper.models`; use `from texas_turnout_scraper.models import ...`
- 🚫 Never buffer the bulk ZIP in memory — ✅ stream to disk with `response.stream()` and write chunks
- 🚫 Never commit to `main` directly — ✅ use feature branches (`git checkout -b feature/{name}`) and open a PR
- 🚫 Never use `print()` for diagnostic output — ✅ use `logging.getLogger(__name__)` with appropriate levels
- 🚫 Never catch bare `except:` — ✅ catch specific exceptions (e.g. `except httpx.HTTPStatusError:`, `except ValueError:`)
- 🚫 Never include voter names or VUIDs in exception messages or tracebacks — ✅ log counts and field names only; redact PII from error context
- 🚫 Never quote third-party library APIs (httpx, Pydantic, FastMCP, DuckDB, etc.) from memory — ✅ use Context7 MCP (`resolve-library-id` then `get-library-docs`) for current API surface
- 🚫 Never use raw `git checkout -b`, `git branch`, or `git merge` — ✅ this project uses GitButler; use `gb branch create/apply/push` (see `docs/GITBUTLER.md`). The `.claude/hooks/block-raw-git.sh` PreToolUse hook will block these.
- 🚫 Never silence `ty` warnings by adding `# type: ignore` without a one-line rationale comment — ✅ if the warning is real but not actionable now, raise it in `docs/adr/007-ty-migration-to-error-mode.md` and add a `# ty: ignore[code]  # reason: ...` directive

---

## Tool Permissions by Mode

This table maps each subagent (defined in `.claude/agents/`) to the tools it may use. Project
hooks under `.claude/hooks/` fire on every tool call regardless of which agent invoked it; the
table here describes the agent's *allowlist*, not its *block list*.

| Agent | Read | Write `src/` | Write `tests/` | Write `docs/` | Bash | Notion write |
|---|---|---|---|---|---|---|
| `code-reviewer` | ✅ | ❌ | ❌ | ❌ | ✅ (read-only commands) | ❌ |
| `test-writer` | ✅ | ❌ | ✅ | ❌ | ✅ (pytest only) | ❌ |
| `researcher` | ✅ | ❌ | ❌ | ✅ (`docs/research/` + HANDOFF.md only) | ✅ | ❌ |
| `session-closer` | ✅ | ❌ | ❌ | ❌ (HANDOFF.md + `.claude/handoffs/` only) | ✅ | ❌ |
| `security-auditor` | ✅ | ❌ | ❌ | ❌ | ✅ (read-only commands) | ❌ |
| `notion-publisher` | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ (sole owner of Notion writes) |
| `task-critic` | ✅ | ❌ | ❌ | ❌ | ✅ (read-only commands) | ❌ |
| `mcp-contract-checker` | ✅ | ❌ | ❌ | ❌ | ✅ (read-only) | ❌ |
| `architecture-guardian` | ✅ | ❌ | ❌ | ❌ | ✅ (read-only) | ❌ |

**Never grant Write `src/` to a checker agent.** Checkers report; they don't fix. Pair a checker with the main agent that can fix, and have the main agent re-invoke the checker after applying changes.

---

## Anti-Pattern Warnings

These are the *failure modes* that have been observed in this codebase and ratcheted into
guardrails (lint rules, hooks, or contract tests). They're separate from `## NEVER DO` because
they're stack-specific patterns rather than absolute prohibitions.

### Stack patterns to avoid

| Anti-pattern | Where it bit us | Guardrail |
|---|---|---|
| MCP tool call keyword names drifted from the underlying client method signature | `mcp_server.py:100, 148, 194` — three of seven tools raise `TypeError` on first call | `tests/verify/check_mcp_tools_have_tests.py` + per-file strict `ty` (see ADR-0007) + `mcp-contract-checker` subagent |
| Bare `except Exception:` with `exc_info=True` in code that touches PII | `roster.py:319` — risks leaking voter names through CSV-parse tracebacks | ruff `BLE` selector + `.claude/hooks/check-pii-exc-info.sh` PostToolUse hook |
| Parallel implementations for civix/legacy that drift in normalization | 22 distinct refactoring issues / 74 occurrences in the 5/25 review | ruff `C901`/`PLR0915`/`PLR0912` + `architecture-guardian` subagent + `docs/playbooks/dual-source-pattern.md` |
| Reaching into another module's `_private` attributes (worse: *mutating* them) | `roster.py:103` mutates `LegacySession._pace_seconds` mid-fetch | ruff `SLF` selector |
| Naive `datetime.utcnow()` or `datetime.now()` without tz | `audit.py:211` — deprecated in 3.12, removed in 3.13 | ruff `DTZ` selector |
| `# type: ignore` without rationale comment, or `tool.ty.rules.all = "warn"` masking real correctness errors | The MCP keyword-arg drift above was silenced by warn-only `ty` | ADR-0007 ratchet plan + per-file `[[tool.ty.overrides]]` strict on `mcp_server.py` first |
| Two functions/modules emit different vocabularies for the same domain condition | Historical split between `audit.py` and removed `writer.audit_from_records` (unified in WS-3) | `tests/unit/test_audit_contract.py` + shared `FindingType` enum |
| Hardcoded base URLs without env-var override | `session.py:43`, `civix.py:42` | Architecture-guardian flag at review time; tracked as strategic item S4 in `prompts/10-review-remediation/current.md` |

### Agent process anti-patterns

- **Don't quote library APIs from memory.** Always Context7 → resolve-library-id → get-library-docs first.
- **Don't redesign a module on a one-line change request.** Per the Learned User Preferences, propose and get approval first.
- **Don't fabricate findings.** Every code review or refactoring claim must cite a real `file:line` ref.
- **Don't merge a feature that adds a new MCP tool without a corresponding test in `tests/unit/test_mcp_server.py`.** The verify-suite check will block CI.

---

## GitButler

This project uses [GitButler](https://gitbutler.com/) for virtual branch management. The prompt-
driven workflow has parallel feature work in flight regularly (multiple subagents on independent
tranches); GitButler tracks per-virtual diffs without forcing `git checkout` switches.

**NEVER DO:** raw `git checkout -b`, `git branch`, or `git merge` — these bypass GitButler's
tracking and create silent drift. The `.claude/hooks/block-raw-git.sh` PreToolUse hook blocks
these subcommands.

**Use the `gb` CLI instead:**

| Operation | Command |
|---|---|
| Create virtual branch | `gb branch create feature/{name}` |
| List active virtuals | `gb branch list` |
| Switch (apply) | `gb branch apply feature/{name}` |
| Push to remote | `gb branch push feature/{name}` |
| Drop unapplied | `gb branch drop feature/{name}` |

Read-only git (`status`, `diff`, `log`, `show`, `blame`) works normally. For genuine native-git
workflows (release tagging, manual upstream sync), bypass the hook with `GITBUTLER_BYPASS=1` for
that single invocation.

Full reference: `docs/GITBUTLER.md`. Rationale for adoption: `docs/adr/006-gitbutler-virtual-branches.md`.

---

## Learned User Preferences

- Prefer incremental fixes on the current implementation; do not redesign modules, swap providers, or change architecture without describing the proposal and getting explicit approval first.
- When the user runs `/review`, report findings only and do not edit code until they explicitly ask to fix (e.g. "Fix", "Fix all").
- Drive implementation from versioned prompts when the user references `@NN-topic` or a name under `prompts/` — read `prompts/{topic}/current.md` (and `v{X.Y.Z}.md` if needed) rather than improvising scope.
- Create git commits only when the user explicitly requests them.
- Prefer parallel subagents for independent multi-module or multi-tranche work (e.g. civix + legacy CLI, review-fix plans).
- When implementing from an attached plan, do not edit the plan file; treat it as read-only scope.
- When a plan already has todos, do not recreate them; mark each `in_progress` then `completed` as you work through the list.
- When asked to fix review findings, run fix → `/review` repeatedly until no issues remain (e.g. “Fix everything” / “until we're all clear”).

## Learned Workspace Facts

- Dev installs and CI use **uv** with a committed **`uv.lock`**; type checking is **`ty`** (`uv run ty check`), not Poetry or mypy.
- `.gitignore` keeps **`data/**/*.csv`** and **`tests/fixtures/**/*.csv`** tracked while ignoring **`HANDOFF.md`**, **`TASK.md`**, **`.claude/handoffs/`**, **`src/texas_turnout_scraper/tmp/`**, and **`.cursor/hooks/state/`**.
- Unit tests under **`tests/unit/`** mock HTTP with **respx** and load synthetic data from **`tests/fixtures/early_voting/`**; they must not hit the live SOS site.
- Work is organized in **`prompts/{topic}/`** (`current.md` plus versioned snapshots); numbered prompts (e.g. `01-test-fixtures`, `02-unit-tests-civix`) map to test/fixture/CLI tranches in the refactor plan.
- **Normalization:** Civix VUIDs use **`.zfill(10)`**; legacy `_parse_county_csv` uses **`str()` only**; voterfile/roster precinct compare uses **`normalize_precinct`** / **`precincts_match`** (SOS zero-padded vs Civix unpadded).
- **`writer.py`** exposes **`ROSTER_CSV_COLUMNS`** for CSV header parity; duplicate **`also_found_on`** uses row-index matching (same county/date duplicates still cross-reference).
- Integration tests require **`--live`** (`tests/conftest.py` skips them otherwise); run with `uv run pytest tests/integration/ -v --live`.
- Full local verification: `uv sync --dev`, ruff `E,W,F,I`, `uv run ty check`, `pytest tests/unit`, `pytest tests/verify`, then optional live integration.
- **CLI:** namespaced **`tx-turnout civix|legacy|audit|voterfile`**; **`fetch-all <id>`** → `data/elections/{civix|legacy}/{id}/roster_ev_{id}.csv`; **`refresh-all`** for batch stale elections; **`civix elections`** uses **questionary** on TTY (`--no-interactive` for CI).
- **HTTP/Typer:** default Civix/legacy HTTP uses **cloudscraper** — catch **`requests.HTTPError`** as well as **`httpx.HTTPError`**; CLI EV dates use string **`EvDateStr`**, not **`Annotated[date]`** (Typer 0.25 registration fails on `date`).
- **Release Please:** `.github/workflows/release-please.yml` + `release-please-config.json`; conventional commits on **`main`** open release PRs updating **`CHANGELOG.md`** and **`pyproject.toml`** version.
- **Audit:** canonical **`audit.audit_records()`** + **`FindingType`** enum (`tests/unit/test_audit_contract.py`); combined EV audits at **`data/elections/{source}/{id}/audit_ev_{id}.json`** via **`writer.stored_audit_ev_path`**.

---

## Changelog

| Version | Date | Change |
|---------|------|--------|
| 1.3.0 | 2026-05-25 | Post-alignment pass: real Notion References (Project + Client + Tasks DB + Docs DB + Skill Run Log); added `## Tool Permissions by Mode`, `## Anti-Pattern Warnings`, `## GitButler` sections; Context7 promoted to top of Documentation Priority + NEVER DO; added ty `# type: ignore` rationale rule and GitButler raw-git prohibition to NEVER DO; documented 8 stack anti-patterns surfaced in the 5/25 review with their guardrails |
| 1.2.0 | 2026-05-24 | Added `legacy_api` facades, `LegacySession.prime_election`, `fetch_ev_details_html`; CLI/MCP wired to facades; RUNBOOK.md; cloudscraper ops documented |
| 1.1.0 | 2026-05-24 | Added version header, Agent Scope, Model Configuration, Session Management archive pattern, paired NEVER DO alternatives, 6-field Notion References; added voterfile/writer modules to layout |
| 1.0.0 | 2026-05-24 | Initial release — httpx refactor, Pydantic v2 models, DuckDB voterfile matching |

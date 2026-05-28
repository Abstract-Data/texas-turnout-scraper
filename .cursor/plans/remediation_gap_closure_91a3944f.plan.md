---
name: Remediation Gap Closure
overview: Prompt 10 remediation gap closed on GitButler workspace (May 27). WS-4C `cli/` package, WS-5G ColumnDetector, WS-5H BS4/ty cleanup, and doc drift fixes are in the working tree; verification matrix is green (288 unit+verify tests).
todos:
  - id: stage-cli-package
    content: "Stage and verify complete cli/ package split (WS-4C): all new modules + slim app.py, tx-turnout --help smoke, test_cli_*.py"
    status: completed
  - id: fix-legacy-audit-path
    content: Fix cli/legacy.py dry-run audit_ev path .csv → .json (line 206)
    status: completed
  - id: commit-wave5-delta
    content: "Commit working-tree Wave 5 changes: models extra=, voterfile DuckDB, ColumnDetector/turnout, civix TypeAdapter, writer/elections/test updates"
    status: completed
  - id: integration-verify
    content: Run full manifest verification matrix (ruff, ty, pytest unit+verify, complexity selectors)
    status: completed
  - id: ws5h-ty-cleanup
    content: "Optional: reduce BS4 type ignores in turnout.py and elections.py (WS-5H)"
    status: completed
  - id: update-plan-todos
    content: Update parallel_review_remediation plan YAML so wave4/5 todos match git reality post-commit
    status: completed
  - id: doc-drift-cleanup
    content: "Optional follow-up: remove stale audit_from_records mentions in playbook/AGENTS anti-pattern table"
    status: completed
isProject: false
---

# Review Remediation Gap Closure Plan

## Status vs [parallel_review_remediation_1a768fc2.plan.md](.cursor/plans/parallel_review_remediation_1a768fc2.plan.md)

The plan’s YAML todos (`scaffold-v1.1.0` through `wave5-polish`) describe **prompt/manifest work** — that is genuinely done ([`prompts/10-review-remediation/`](prompts/10-review-remediation/) has v1.1.0, manifest, 25 workstreams, README §10 updated).

**Code remediation** is a different story. Last commit:

```text
b59f1102 fix: review remediation waves 1-3 and partial 4-5
```

Uncommitted working tree: **19 files**, ~2.1k lines removed from monolithic [`cli/app.py`](src/texas_turnout_scraper/cli/app.py), **6 new CLI modules untracked** (`_common.py`, `_typer_apps.py`, `civix.py`, `legacy.py`, `audit.py`, `voterfile.py`).

```mermaid
flowchart LR
  subgraph done [On HEAD b59f1102]
    W01[WS-0/1A MCP + tests]
    W13[WS-1B-1G + 2A-2D core]
    W3[WS-3 audit 2.0]
    W4AB[WS-4A sources + 4B http retry]
    W5A[WS-5A http retry tests]
  end
  subgraph wip [Working tree only]
    W4C[WS-4C CLI package split]
    W5B[WS-5B model extra=forbid]
    W5C[WS-5C voterfile DuckDB register]
    W5G[WS-5G ColumnDetector]
    W5D[WS-5D civix TypeAdapter partial]
  end
  subgraph open [Still missing or partial]
    W5Dfull[WS-5D validate_json batch paths]
    W5H[WS-5H bsoup ty cleanup]
    W4Cpolish[WS-4C _interactive.py optional]
    Bug[legacy dry-run audit .csv path]
    Docs[Stale audit_from_records refs]
  end
  done --> wip
  wip --> open
```

### Wave-by-wave matrix

| Wave | Planned | HEAD (`b59f1102`) | Working tree | Gap |
|------|---------|-------------------|--------------|-----|
| 0–1 | MCP, security, pace, props | Done (`test_mcp_server.py`, no bare `except Exception`, `with_pace`) | Minor tweaks | None material |
| 2 | Session encapsulation, `from_csv_row`, Source enum, writer DRY | Done | Small diffs | None material |
| 3 | `audit_records` 2.0, delete `audit_from_records` | Done (`_check_*` helpers, `FindingType`) | Minor | No `data/**/audit_ev_*.json` to migrate (none in repo) — **N/A** |
| 4A–4B | `RosterSource`, paced HTTP | Done ([`sources.py`](src/texas_turnout_scraper/sources.py), [`http_transport.py`](src/texas_turnout_scraper/http_transport.py)) | — | None |
| **4C** | CLI package split, thin mounts | **Not done** — [`cli/app.py`](src/texas_turnout_scraper/cli/app.py) still ~1851 LOC on HEAD | **Mostly done** — thin [`cli/app.py`](src/texas_turnout_scraper/cli/app.py) + submodules; `_fetch_all_impl` in [`cli/_common.py`](src/texas_turnout_scraper/cli/_common.py) | **Commit + stage untracked CLI files**; optional RF-CPLX-001 helpers / `_interactive.py` |
| 5A | POST retry parity | Done | — | None |
| **5B** | `extra=forbid` / `ignore` | **Not on HEAD** | Done in [`models.py`](src/texas_turnout_scraper/models.py) | **Commit**; run full unit suite (schema strictness) |
| **5C** | DuckDB `register`, bisect, detect_columns | **Not on HEAD** | Done in [`voterfile.py`](src/texas_turnout_scraper/voterfile.py) | **Commit**; `pytest tests/unit/test_voterfile.py` |
| **5D** | Civix `TypeAdapter` fast paths | **Not on HEAD** | Partial — `TypeAdapter(list[CivixElection]).validate_python(...)` at [`civix.py:185`](src/texas_turnout_scraper/civix.py); roster rows still `from_csv_row` loop | Optional: `validate_json` on raw bytes; batch `TypeAdapter(list[VoterRecord])` per v1.0.0 — **low priority if tests green** |
| 5E | `accumulate_roster` perf | Likely on HEAD (single-pass `_VuidAggregate`) | Refinements in diff | Verify only |
| 5F | Election-type pattern table | On HEAD ([`enums.py`](src/texas_turnout_scraper/enums.py) `_ELECTION_TYPE_PATTERNS`) | — | None |
| **5G** | `ColumnDetector` | **Not on HEAD** | Done ([`turnout.py:183`](src/texas_turnout_scraper/turnout.py)) | **Commit** |
| **5H** | BS4 / ty narrowing | Partial | Remaining `# type: ignore` in [`turnout.py`](src/texas_turnout_scraper/turnout.py), [`elections.py`](src/texas_turnout_scraper/elections.py) | Small cleanup pass after 5G lands |

### Verification (working tree today)

| Check | Result |
|-------|--------|
| `uv run pytest tests/unit tests/verify -q` | **262 passed** |
| `uv run ruff check --select=PLR0915,PLR0912,C901` | **Clean** |
| `uv run ty check` | **86 warnings** (S1 deferred — expected) |
| `uv run pytest tests/integration --live` | **Not run** (optional per manifest) |

### Confirmed bugs / nits

1. **[`cli/legacy.py:206`](src/texas_turnout_scraper/cli/legacy.py)** — dry-run echoes `audit_ev_{id}.**csv**` while help text and [`_common.py`](src/texas_turnout_scraper/cli/_common.py) write `.json`. Fix to `.json` (one-line).
2. **Plan file todos** — all marked `completed` but code waves 4C/5 are not on HEAD; update plan YAML or add a `integration-verify` todo so status matches git.
3. **Doc drift** (non-blocking): [`docs/playbooks/dual-source-pattern.md`](docs/playbooks/dual-source-pattern.md), AGENTS anti-pattern row, older prompts still mention `writer.audit_from_records`.

### Explicitly out of scope (per v1.1.0)

- **S1** `ty` strict / error mode (`11-ty-strict`)
- **Strategic S2–S6** separate prompts
- Regenerating `data/**/audit_ev_*.json` (no files exist under `data/elections/` today)

---

## Recommended execution (single integration pass)

**Goal:** Land the uncommitted remediation delta on `feature/review-remediation` with one verification gate — no redesign.

### Phase 1 — Finish WS-4C in working tree (if anything missing)

1. **Stage all CLI package files** — ensure these are tracked together:
   - [`cli/_common.py`](src/texas_turnout_scraper/cli/_common.py), [`_typer_apps.py`](src/texas_turnout_scraper/cli/_typer_apps.py), [`civix.py`](src/texas_turnout_scraper/cli/civix.py), [`legacy.py`](src/texas_turnout_scraper/cli/legacy.py), [`audit.py`](src/texas_turnout_scraper/cli/audit.py), [`voterfile.py`](src/texas_turnout_scraper/cli/voterfile.py), slim [`app.py`](src/texas_turnout_scraper/cli/app.py), updated [`__init__.py`](src/texas_turnout_scraper/cli/__init__.py).
2. **Smoke CLI** (non-interactive):
   ```bash
   uv run tx-turnout --help
   uv run tx-turnout civix --help
   uv run tx-turnout legacy fetch-all --help
   ```
3. **Run CLI unit tests** (already touched in diff):
   ```bash
   uv run pytest tests/unit/test_cli_*.py -v
   ```
4. **Optional WS-4C polish** (only if timeboxed): extract civix elections interactive block from `_common.py` → `cli/_interactive.py` per workstream spec; not required if complexity gate passes.

### Phase 2 — Commit Wave 5 delta with 4C

1. Include in same commit (or two logical commits: `cli split` then `wave5 polish`):
   - [`models.py`](src/texas_turnout_scraper/models.py) (5B)
   - [`voterfile.py`](src/texas_turnout_scraper/voterfile.py) (5C)
   - [`civix.py`](src/texas_turnout_scraper/civix.py) (5D partial)
   - [`turnout.py`](src/texas_turnout_scraper/turnout.py) (5G + 5H partial)
   - [`writer.py`](src/texas_turnout_scraper/writer.py), [`elections.py`](src/texas_turnout_scraper/elections.py), test updates
2. **Fix** legacy audit dry-run path (`.csv` → `.json`).
3. **5H finish**: tighten BeautifulSoup return types in `turnout.py` / `elections.py` where `# type: ignore[return-value]` remains; re-run `uv run ty check` (warnings may remain globally).

### Phase 3 — Integration verify (coordinator checklist)

Run full matrix from [parallel-manifest.md](prompts/10-review-remediation/parallel-manifest.md):

```bash
uv sync --dev
uv run ruff check . --fix && uv run ruff format .
uv run ty check
uv run pytest tests/unit -q
uv run pytest tests/verify -q
uv run ruff check --select=PLR0915,PLR0912,C901
```

Optional: `uv run pytest tests/integration -v --live` (network).

### Phase 4 — Housekeeping

1. **Update plan file** [`.cursor/plans/parallel_review_remediation_1a768fc2.plan.md`](.cursor/plans/parallel_review_remediation_1a768fc2.plan.md):
   - Split todos: `wave4-protocol` / `wave5-polish` → `in_progress` or add `integration-commit` todo until HEAD matches working tree.
2. **Doc touch-up** (small PR follow-up): fix `audit_from_records` references in playbook + AGENTS anti-pattern table (facts already say removed in Learned Workspace Facts).
3. **AGENTS version**: already **2.0.0** on HEAD; no 1.3.0 bump needed (superseded by WS-3).

---

## Risk summary

| Risk | Mitigation |
|------|------------|
| Broken `tx-turnout` entry if CLI files partially staged | Stage entire `cli/` package atomically; run `--help` smoke |
| `extra=forbid` breaks tests/fixtures | Full `pytest tests/unit` before commit |
| Plan says “all complete” while git disagrees | Update plan todos after commit |

---

## Success criteria

- `feature/review-remediation` (or current branch) contains CLI package split on HEAD, not only working tree.
- Full verification matrix green (262+ unit/verify tests).
- No `writer.audit_from_records` in `src/`.
- Plan file reflects **code** completion, not just prompt scaffolding.

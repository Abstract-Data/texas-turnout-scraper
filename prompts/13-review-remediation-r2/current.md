# review-remediation-r2 — 2026-05-26 Review Fix Plan (active)
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-26
# Maintainer: John Eakin

# Prompt 13 — Review Remediation R2 (current)

**Active edition:** This file tracks **v1.0.0**. Full plan: [`v1.0.0.md`](v1.0.0.md).

This is the **second round** of review-remediation. Prompt `10-review-remediation` covered the
2026-05-25 reports and closed the latent MCP-keyword-drift bug, the `_pace_seconds` mutation,
and the naive `utcnow()` issue (verified fixed in code at `mcp_server.py:100,148,194`,
`session.py:152-160`, and `audit.py:245`). This prompt picks up the remaining 13 RF-* issues
plus the new findings from the 2026-05-26 sweep, with the focus the user requested: **precise
decomposition of god modules and god functions**, grounded in the existing
`RosterSource` Protocol and the dual-source playbook.

## Quick links

| Artifact | Purpose |
|----------|---------|
| [`v1.0.0.md`](v1.0.0.md) | Full plan: phases, module layouts, function signatures, verify matrix |
| `docs/playbooks/dual-source-pattern.md` | Heuristic for what to share between civix/legacy |
| `docs/adr/008-rostersource-protocol.md` | The Port + decision rationale |
| `docs/adr/007-ty-migration-to-error-mode.md` | ty strict-mode ratchet plan |

## Notion reports (2026-05-26 run)

- **Code Review** — https://www.notion.so/36c7d7f56298816c99bcddc3eb4a6ff8 (Overall 6.4/10)
- **Refactoring & Code Smell** — https://www.notion.so/36c7d7f56298814b949ff9b5fd946339 (14 distinct issues / 62 occurrences)
- **Developer Assessment** — https://www.notion.so/36c7d7f56298814b9452c4b046b30733 (Senior, AI-Assisted Expert, ratio 2.14:1)

## Execution shape

```
Phase 0 — Ground-truth verification (30 min, no code changes)
  ↓
Phase 1 — Quick Wins (~3 hours; 7 small issues that don't touch cli.py structure)
  ↓
Phase 2 — Dual-source consolidation: wire cli.py through the existing RosterSource Protocol (1.5 days)
  ↓
Phase 3 — cli.py → cli/ package split + voterfile_match decomposition (2 days)
  ↓
Phase 4 — Hardening: ty strict overrides, `extra="forbid"`, env-var BASE_URL, DuckDB security, Source enum (1 day)
  ↓
Phase 5 — Strategic tooling install set from Developer Assessment (parallel; not required to declare done)
```

Each phase ends with **`uv run ruff check . --fix && uv run ruff format . && uv run ty check && uv run pytest tests/unit -q && uv run pytest tests/verify -q`** green before the next phase starts.

## Branch model

This project uses GitButler. Open one virtual branch per phase:

```
gb branch create feature/review-r2-phase-1-quick-wins
gb branch create feature/review-r2-phase-2-rostersource-wiring
gb branch create feature/review-r2-phase-3-cli-package-split
gb branch create feature/review-r2-phase-4-hardening
gb branch create feature/review-r2-phase-5-tooling  # optional
```

Phase 3 has two large sub-branches (cli package + voterfile_match) that may merge separately.

## Dispatch

Open `v1.0.0.md`. Work phase-by-phase, top to bottom. Do not improvise scope — every target
module path, function signature, and file:line ref is in the plan.

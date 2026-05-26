# review-remediation — Parallel Multi-Agent Edition (active)
# Version: 1.1.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-25
# Maintainer: John Eakin

# Prompt 10 — Review Remediation (current)

**Active edition:** This file tracks **v1.1.0** (parallel workstreams). Full index:
[`v1.1.0.md`](v1.1.0.md)

**Frozen sequential baseline:** [`v1.0.0.md`](v1.0.0.md) — do not edit.

## Quick links

| Artifact | Purpose |
|----------|---------|
| [`v1.1.0.md`](v1.1.0.md) | Parallel edition index, wave table, coordinator notes |
| [`parallel-manifest.md`](parallel-manifest.md) | DAG, file locks, branch names, merge train, verification matrix |
| [`workstreams/`](workstreams/) | One self-contained agent brief per workstream (full mechanical specs) |

## Execution (parallel waves)

```
WS-0 (optional) → (WS-1A..1G parallel) → WS-1H → WS-2A → WS-2B → (WS-2C ‖ WS-2D) → WS-3
  → (WS-4A ‖ WS-4B) → WS-4C → (WS-5A|5B|5C|5E|5F parallel) → WS-5D → WS-5G → WS-5H → VERIFY
```

Integration branch: `feature/review-remediation`

Workstream branches: `feature/review-remediation/ws-{slug}` — see [manifest](parallel-manifest.md#branch-naming).

## Goal (summary)

Apply every actionable finding from the 2026-05-25 review suite using file-locked workstreams
and a merge train — same scope as v1.0.0, different dispatch model.

**Notion reports:**

- Code Review — https://www.notion.so/36c7d7f5629881568bddf40b099c2979
- Refactoring & Code Smell — https://www.notion.so/36c7d7f56298816a9f2af0f0b7169cde
- Developer Assessment — https://www.notion.so/36c7d7f5629881e1b2faf76bed72972e

## Files to read first

1. `AGENTS.md`
2. [`parallel-manifest.md`](parallel-manifest.md)
3. Your assigned [`workstreams/WS-*.md`](workstreams/)

## Strategic follow-ups (out of scope)

S1–S6 from v1.0.0 → separate prompts (`11-ty-strict`, `12-health-json`, …). Listed in
[`v1.1.0.md`](v1.1.0.md#strategic-items-out-of-parallel-waves).

## Dispatch

Open the workstream file for your WS ID. Do not improvise scope from memory — specs are
copied from v1.0.0 into each workstream file.

**Example (WS-1A):**

> Read `workstreams/WS-1A-mcp.md`. Branch `feature/review-remediation/ws-1a-mcp`. Do not edit `cli.py`.

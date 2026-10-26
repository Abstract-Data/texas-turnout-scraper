# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for texas-turnout-scraper.

## Format

Each ADR is a Markdown file named `NNN-short-title.md`:

```
# ADR-NNN: Title

**Date:** YYYY-MM-DD
**Status:** Proposed | Accepted | Deprecated | Superseded by ADR-NNN

## Context
What is the issue motivating this decision?

## Decision
What was decided?

## Consequences
What are the trade-offs and outcomes?
```

## Records

| # | Title | Status | Date |
|---|-------|--------|------|
| [001](./001-httpx-over-selenium.md) | Use httpx instead of Selenium | Accepted | 2026-05-24 |
| [002](./002-pydantic-over-sqlmodel.md) | Pydantic v2 models (no SQLModel) | Accepted | 2026-05-24 |
| [003](./003-github-pages-static-api.md) | GitHub Pages as static JSON/CSV API | Accepted | 2026-05-24 |
| [004](./004-civix-evr-source.md) | Add Civix EVR API as second data source | Accepted | 2026-05-24 |
| [005](./005-release-please.md) | Automated releases with Release Please | Accepted | 2026-05-25 |
| [009](./009-sqlmodel-roster-models.md) | SQLModel models for D1 roster rows | Accepted | 2026-10-23 |
| [010](./010-geography-tables.md) | Geography tables in D1 | Accepted | 2026-10-24 |
| [011](./011-voter-attributes.md) | Voter attributes, history and scores in D1 | Accepted | 2026-10-24 |
| [012](./012-repeat-vuids.md) | Repeat VUIDs and excess counts in D1 | Accepted | 2026-10-24 |
| [013](./013-worker-cron-dispatches-sync.md) | Worker cron dispatches the early-vote sync | Accepted | 2026-10-24 |
| [014](./014-cloudflare-workflow-ingest.md) | Early-vote ingest as a Cloudflare Workflow | Proposed | 2026-10-26 |

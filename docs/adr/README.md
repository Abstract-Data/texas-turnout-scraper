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

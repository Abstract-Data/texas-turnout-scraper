# ADR-003: GitHub Pages as static JSON/CSV API

**Date:** 2026-05-24
**Status:** Accepted

## Context
We want scraped data to be accessible to anyone who clones the repo and to AI agents
via simple HTTP GET, without running a server.

## Decision
Commit CSV/JSON output to `data/elections/` in the repo. GitHub Actions runs
`tx-turnout` on a schedule and commits new data. GitHub Pages serves `data/` as a
static API: `https://{org}.github.io/texas-turnout-scraper/data/elections/index.json`.

The Flat Viewer (flat.githubocto.com) can read any committed CSV/JSON via its viewer
URL — no Flat Action needed since the Flat Action only supports simple GET (incompatible
with the stateful SOS POST session).

## Consequences
- No server to maintain
- Data latency = scheduled job cadence (daily)
- Repo size grows with each scrape — consider sparse checkout for large deployments
- `.gitignore` must have `!data/**/*.csv` exception (added)

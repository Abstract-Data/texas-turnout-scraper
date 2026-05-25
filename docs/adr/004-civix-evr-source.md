# ADR-004: Add Civix EVR API as Second Election Data Source

**Date:** 2026-05-24
**Status:** Accepted

## Context

The Texas SOS has migrated early-voting report infrastructure to a new Civix-hosted portal
at `goelect.txelections.civixapps.com`. The new site carries elections from 2025 onward.
The legacy Java/Struts portal at `earlyvoting.texas-election.com` remains available for
pre-2025 historical data but is not being updated with new elections.

## Decision

Add a `civix.py` module implementing the Civix EVR API as a second ingest source.
The two sources serve different date ranges:

| Source | Coverage | Module |
|--------|----------|--------|
| Legacy SOS (`earlyvoting.texas-election.com`) | Pre-2025 historical | `session.py`, `roster.py`, `turnout.py` |
| Civix EVR (`goelect.txelections.civixapps.com`) | 2025+ (current) | `civix.py` |

The Civix API is **dramatically simpler** — stateless GET requests, JSON responses, no
session management. It does not replace the legacy modules (which handle historical data),
it supplements them.

## Consequences

- New module `civix.py` added to core library
- New CLI subcommand group: `tx-turnout civix elections list`, `tx-turnout civix roster fetch`, etc.
- New MCP tools: `civix_list_elections`, `civix_fetch_roster`, `civix_fetch_turnout`
- All Civix responses decode a `{"upload": "<base64>"}` envelope before parsing
- `source_election_id` for Civix elections is `str(id)` (Civix integer ID) — different
  namespace from legacy SOS IDs, but same type convention (always string)
- Data stored under `data/elections/civix/{source_election_id}/`  to avoid collision
  with legacy data at `data/elections/{source_election_id}/`

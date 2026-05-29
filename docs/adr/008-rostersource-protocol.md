# ADR 008: `RosterSource` protocol for civix/legacy dual sources

**Date:** 2026-05-25
**Status:** proposed (decision; implementation tracked in `prompts/10-review-remediation/`)

## Context

The 2026-05-25 Refactoring Report identified the civix/legacy parallel implementations as the
single biggest refactoring opportunity (RF-DRY-001 — Critical):

- `cli/civix.py` (`fetch_all`) ≅ `cli/legacy.py` (`fetch_all`) — parallel fetch-all flows
- `cli/civix.py` (`refresh_all`) ≅ `cli/legacy.py` (`refresh_all`) — parallel refresh flows
- `cli/_common.py` (`_build_civix_index_entries`) ≅ `cli/_common.py` (`_build_legacy_index_entries`)
- The two CSV row parsers in `civix.py:317–332` and `roster.py:262–299` independently
  normalize VUIDs and voting methods, with subtle behavior differences
- Before WS-3, audit findings used inconsistent `finding_type` strings across modules (RF-ARCH-001).
  WS-3 unified on `audit.audit_records()` and the `FindingType` enum.

The root cause isn't that the developer wrote duplicate code carelessly — it's that there's
no shared abstraction for "an EV roster source". Each source's quirks (Civix uses
`roster_available` to pre-filter; Legacy must call `fetch_ev_details_html` first) were handled
inline in the CLI subcommands rather than behind an interface.

## Decision

Introduce a `RosterSource` Protocol that captures the minimum shared API both sources implement:

```python
from typing import Protocol, Sequence
from datetime import date

class RosterSource(Protocol):
    source_prefix: str  # "civix" | "legacy"

    def list_elections(self) -> Sequence[ElectionSummary]: ...

    def resolve_county_ids(
        self,
        election: ElectionSummary,
        election_date: date,
    ) -> Sequence[CountyId]: ...

    def fetch_election_rosters(
        self,
        election: ElectionSummary,
        *,
        pace_seconds: float = 1.0,
    ) -> tuple[list[VoterRecord], list[CountyFetchFailure], ElectionMeta]: ...
```

Two implementations:
- `CivixSource` — wraps `CivixClient`; uses `roster_available` pre-filter
- `LegacySource` — wraps `LegacySession`; calls `fetch_ev_details_html` + `extract_county_ids`
  inside `resolve_county_ids`

Three CLI helpers consume the protocol:
- `_fetch_all_impl(source: RosterSource, election_id, ...) -> int`
- `_refresh_all_impl(source: RosterSource, ...) -> int`
- `_build_index_entries(output_dir, elections, source_prefix, election_date_fn, ...) -> list[IndexEntry]`

Each Typer subcommand becomes a 5-line dispatcher that constructs the right source and calls
the shared impl.

## When NOT to share

Not everything civix/legacy should share. Keep separate:
- The HTTP wire protocol (Civix is REST + base64 envelope; Legacy is Java/Struts + JSESSIONID)
- The HTML/CSV parsing (Civix returns CSVs; Legacy parses HTML)
- The session-establishment protocol (Civix is stateless; Legacy is three-step priming)

The split is at the *roster-source* boundary, not the *HTTP-client* boundary. Trying to share
the HTTP layer (as some early prototypes did) collapses two incompatible protocols and produces
the worst of both worlds.

See `docs/playbooks/dual-source-pattern.md` for the heuristic.

## Consequences

**Easier:**
- New source (e.g. county-level direct scrapes) requires implementing one Protocol, not
  reimplementing four CLI commands.
- New CLI feature (e.g. `--output json`, `--include-mail-only`) is written once and applies
  to both sources.
- The vocabulary-divergence class of bug (RF-ARCH-001 for audit; same risk for any future
  shared metric) becomes impossible at the protocol boundary.

**Harder:**
- The Protocol signature commits us to a particular shape — adding a new method later is a
  breaking change for any external implementations. Mitigated by keeping the Protocol small
  and adding optional methods as needed.
- Refactoring the existing CLI duplication requires careful preservation of per-source quirks
  (Civix's `roster_available` pre-filter, Legacy's HTML priming). Phase 4 of the
  remediation prompt enumerates these.

**Trade-offs considered:**
- *Abstract base class instead of Protocol:* harder to retrofit external implementations; also
  forces inheritance which is the wrong relationship here (the sources don't share
  implementation).
- *Strategy pattern with closure:* works but harder to type-check; loses introspection.
- *Keep them separate:* the 22-issue / 74-occurrence refactoring report is the cost of this
  option. Not viable post-review.

## Implementation tracking

Phase 4 of `prompts/10-review-remediation/current.md` implements this ADR. The playbook
`docs/playbooks/dual-source-pattern.md` documents the heuristic for future dual-source
decisions.

## References

- 2026-05-25 Refactoring Report — RF-DRY-001 (Critical), RF-ARCH-001 (Critical)
- `docs/playbooks/dual-source-pattern.md`
- `prompts/10-review-remediation/`

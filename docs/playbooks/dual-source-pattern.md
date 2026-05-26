# Playbook: Dual-Source Pattern

**Use when:** you're about to add a third data source, or you're tempted to copy a function from
`civix.py` into a new module, or you're writing the second variant of a CLI command and the
first one already exists.

**Reference ADR:** [`008-rostersource-protocol.md`](../adr/008-rostersource-protocol.md).

## The heuristic

Before writing parallel implementations, ask:

| Question | If yes → share | If no → split |
|---|---|---|
| Do both sources produce the **same output type**? (e.g. `VoterRecord`, `CountyRoster`) | ✅ share at the protocol boundary | ✗ |
| Do both sources have the **same domain semantics**? (e.g. "fetch one county's roster for an EV date") | ✅ share | ✗ |
| Does the **wire format** differ? (REST vs HTML, stateless vs stateful session) | ✗ — keep parsers separate | ✅ split |
| Does the **authentication/session model** differ? | ✗ — keep clients separate | ✅ split |

Concretely for this codebase:

| Layer | Civix | Legacy | Shared? |
|---|---|---|---|
| HTTP transport | `PacedHttpClient` (cloudscraper) | `PacedHttpClient` (cloudscraper) | ✅ already shared (`http_transport.py`) |
| Session model | Stateless (auth headers) | Stateful (JSESSIONID priming) | ❌ split |
| Wire format | JSON envelope + base64 CSV | HTML pages + form POSTs | ❌ split |
| Domain output | `VoterRecord`, `CountyRoster` | `VoterRecord`, `CountyRoster` | ✅ shared (`models.py`) |
| Per-county fetch loop | Iterate Civix county IDs | Iterate Legacy county IDs (from priming) | ✅ share via `RosterSource` protocol |
| `fetch-all` / `refresh-all` CLI | ✓ | ✓ | ✅ share via `_fetch_all_impl(source: RosterSource)` |
| Index entry builder | ✓ | ✓ | ✅ share via `_build_index_entries(source_prefix, …)` |
| Audit | ✓ | ✓ | ✅ share — **single** `audit.audit_records` entry point |

The line is roughly: **wire protocol = split; domain logic = share**.

## The RosterSource protocol

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

Why these three methods and not more?

- **`list_elections`** is needed by both `*_refresh_all` workflows and the interactive
  `civix/legacy elections` browse menus. Both sources expose it; the return type is the
  shared `ElectionSummary`.
- **`resolve_county_ids`** absorbs the per-source quirk (Civix uses `roster_available`
  pre-filter; Legacy must call `fetch_ev_details_html`). Without it, the per-source quirks
  would leak into `_fetch_all_impl`.
- **`fetch_election_rosters`** is the actual fetch loop. The return is a tuple of
  (records, failures, meta) — failures are surfaced separately so the CLI can decide
  whether to `_exit_on_partial_fetch_failures`.

## When NOT to share

You're adding a method like `fetch_signature_image(vuid)` that only Civix supports. Don't add
it to the Protocol — only Civix implementations have it. Either:

1. Make a separate `CivixOnlyClient` interface, OR
2. Add it as an optional method on `CivixSource` only and have callers check `isinstance(source, CivixSource)` before calling it.

If you find yourself implementing a no-op `fetch_signature_image` on `LegacySource` to satisfy
the Protocol, the method doesn't belong on the Protocol.

## When to introduce a third source

If a new data source comes online (e.g. county-level direct scrapes from
`{county}.txelections.org/early-voting.csv`), the steps are:

1. Write `CountyDirectSource(RosterSource)` — implements the three methods.
2. Register a new CLI subapp: `tx-turnout county-direct fetch-all <county>`. The subcommand
   is 5 lines, calling `_fetch_all_impl(CountyDirectSource(...), ...)`.
3. No changes to `_fetch_all_impl`, `_refresh_all_impl`, `_build_index_entries`,
   `audit.audit_records`, or any downstream module.

If you find yourself making changes to those shared functions to accommodate the new source,
the Protocol shape is wrong. Stop and revisit.

## Audit vocabulary

A related class of dual-source drift: the audit pipeline. `audit.py` and
`writer.audit_from_records` historically emitted *different* `finding_type` strings for the
same condition (e.g. `duplicate_vuid` vs `multiple_counties`). The fix is the same shape as
above:

- One canonical entry point: `audit.audit_records(records, *, turnout=None) -> AuditReport`
- `finding_type` is a `Literal[...]` (or `Enum`) so the set is closed
- `tests/unit/test_audit_contract.py` pins the vocabulary so future drift is a CI failure

## Smell checklist (when reviewing PRs)

- [ ] Are there two functions in this PR that differ only by a hardcoded string ("civix" vs "legacy")? → consolidate via the protocol.
- [ ] Does this PR introduce a new field on `VoterRecord` that's only meaningful for one source? → put it on a source-specific subtype, not the shared model.
- [ ] Does this PR add a method to `CivixClient` and a method with the same name to `LegacySession`? → check whether it belongs on `RosterSource` instead.
- [ ] Does this PR catch a `finding_type` string in a string comparison? → it should compare against the `Literal`/`Enum` member, not the string.

## References

- ADR: [`008-rostersource-protocol.md`](../adr/008-rostersource-protocol.md)
- 2026-05-25 Refactoring Report (Notion): RF-DRY-001, RF-DRY-002, RF-ARCH-001
- `prompts/10-review-remediation/` — Phase 4 implements this protocol
- `tests/unit/test_audit_contract.py` — the vocabulary guard

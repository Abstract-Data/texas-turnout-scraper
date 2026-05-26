# WS-4A — RosterSource protocol
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-4a-protocol` |
| Base | `feature/review-remediation` (after WS-3) |
| Model | claude-sonnet-4-6 |
| Wave | 4a |
| Exec | parallel (with WS-4B) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/sources.py` (new)
- Thin adapter changes in `civix.py`, `legacy_api.py` only

### Forbidden (MUST NOT touch)

- `cli.py` body / fetch-all collapse (WS-4C)
- `http_transport.py` pacing (WS-4B)

### Close issues

- RF-DRY-001 (protocol half)

### On conflict

Stop if editing `cli.py` beyond imports.

## Verification subset

```bash
uv run pytest tests/unit -q
uv run ty check
```

## Mechanical spec (from v1.0.0)

### Define `RosterSource` protocol

**File:** new `src/texas_turnout_scraper/sources.py` (or add to existing module).

```python
from typing import Protocol

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

Implement `CivixSource` (wraps `CivixClient`) and `LegacySource` (wraps `LegacySession`).
Preserve the existing per-source quirks behind the protocol:

- Civix: filters by `roster_available` before fetching.
- Legacy: calls `fetch_ev_details_html` + `extract_county_ids` first.

**WS-4C** collapses CLI functions onto this protocol.

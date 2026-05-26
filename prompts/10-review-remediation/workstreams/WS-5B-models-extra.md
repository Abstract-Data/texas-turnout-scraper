# WS-5B — Pydantic extra= defaults
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5b-models-extra` |
| Base | `feature/review-remediation` (after WS-4C) |
| Model | claude-sonnet-4-6 |
| Wave | 5 |
| Exec | parallel (must merge before WS-5D) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/models.py` (`model_config` / `extra=` only)

### Forbidden (MUST NOT touch)

- `civix.py` (WS-5D)

### Close issues

- P3-CODE-001

## Verification subset

```bash
uv run ty check
uv run pytest tests/unit -q
```

## Mechanical spec (from v1.0.0)

### P3-CODE-001 — Pydantic `extra=` defaults

**File:** `src/texas_turnout_scraper/models.py`

Add `extra="forbid"` on every internal model (`VoterRecord`, `CountyRoster`,
`CountyTurnout`, `AuditReport`, `AuditFinding`, `EnrichedVoterRecord`,
`VoterfileMatchReport`, `ColumnMapping`) and `extra="ignore"` on every external-API
type (`CivixElection`, `CivixCountyTurnout` and any other Civix/SOS response wrappers).

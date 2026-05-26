# WS-1C — Audit small fixes
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1c-audit-small` |
| Base | `feature/review-remediation` |
| Model | claude-haiku-4-5 |
| Wave | 1 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/audit.py` only

### Forbidden (MUST NOT touch)

- `writer.py` (WS-3 owns audit consolidation)
- `models.py` FindingType changes

### Close issues

- P2-CODE-001
- RF-DEAD-002
- RF-SMELL-006 (partial — utcnow only)

### On conflict

Stop if `audit.py` has out-of-scope edits; WS-3 will rewrite this module.

## Verification subset

```bash
uv run pytest tests/unit/test_audit.py -q
uv run ty check
```

## Mechanical spec (from v1.0.0)

### P2-CODE-001 — Replace deprecated `datetime.utcnow()` (RF-SMELL-006)

**File:** `src/texas_turnout_scraper/audit.py:211`

```python
# Was: generated_at=datetime.utcnow()
generated_at=datetime.now(timezone.utc)
```

`writer.py` already defines `_utc_now()` at module level — prefer reusing that for
consistency.

### RF-DEAD-002 — Drop the spurious `from datetime import date` in `audit.py`

**File:** `src/texas_turnout_scraper/audit.py:14`

Unused after the local re-import at `:200`. Consolidate to one top-level
`from datetime import datetime, timezone` and drop the bare `date` import.

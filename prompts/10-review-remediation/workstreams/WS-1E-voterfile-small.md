# WS-1E — Voterfile callable annotation
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1e-voterfile-small` |
| Base | `feature/review-remediation` |
| Model | claude-haiku-4-5 |
| Wave | 1 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/voterfile.py` — line ~297 annotation only

### Forbidden (MUST NOT touch)

- Rest of `voterfile.py` (WS-5C owns DuckDB / detect_columns / bisect)

### Close issues

- P2-CODE-002

### On conflict

Stop if diff touches more than ~10 lines outside the annotation.

## Verification subset

```bash
uv run ty check
```

## Mechanical spec (from v1.0.0)

### P2-CODE-002 — Fix `callable` mis-annotation (already-known ty miss)

**File:** `src/texas_turnout_scraper/voterfile.py:297`

```python
# Was: progress_callback: callable | None = None,
from collections.abc import Callable
...
progress_callback: Callable[[], None] | None = None,
```

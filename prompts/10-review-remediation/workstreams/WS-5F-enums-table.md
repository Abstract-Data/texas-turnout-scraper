# WS-5F — Table-driven infer_election_type
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5f-enums-table` |
| Base | `feature/review-remediation` (after WS-4C) |
| Model | claude-haiku-4-5 |
| Wave | 5 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/enums.py` (`infer_election_type` only)

### Forbidden (MUST NOT touch)

- `Source` enum (WS-2D done)

### Close issues

- RF-SMELL-004

## Verification subset

```bash
uv run pytest tests/unit -q -k election_type
uv run ty check
```

## Mechanical spec (from v1.0.0)

### RF-SMELL-004 — Table-drive `infer_election_type`

**File:** `src/texas_turnout_scraper/enums.py:48-70`

```python
_PATTERNS: tuple[tuple[str, ElectionType], ...] = (
    ("primary runoff", ElectionType.PRIMARY_RUNOFF),
    ("primary", ElectionType.PRIMARY),
    ...
)

def infer_election_type(name: str) -> ElectionType:
    lower = name.lower()
    for needle, etype in _PATTERNS:
        if needle in lower:
            return etype
    return ElectionType.OTHER
```

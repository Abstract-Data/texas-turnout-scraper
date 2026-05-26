# WS-1F — Pace floor in session/civix __init__
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1f-pace-init` |
| Base | `feature/review-remediation` |
| Model | claude-sonnet-4-6 |
| Wave | 1 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/session.py` (`LegacySession.__init__` pace floor)
- `src/texas_turnout_scraper/civix.py` (`CivixClient.__init__` pace floor only)

### Forbidden (MUST NOT touch)

- `cli.py` (WS-1H drops CLI duplicates)
- `roster.py:103` mid-fetch mutation (WS-2A `with_pace`)

### Close issues

- RF-DRY-007 (partial — constructors only)

### On conflict

Stop on `session.py` if WS-2A already started; merge 1F before 2A.

## Verification subset

```bash
uv run pytest tests/unit/test_legacy.py tests/unit/test_civix.py -q
```

## Mechanical spec (from v1.0.0)

### RF-DRY-007 — Centralize the `max(pace, 1.0)` floor (partial)

**Scope for WS-1F:** constructors only.

The 1.0-second pacing floor is repeated at 5 call sites. Move it into the
`LegacySession.__init__` and `CivixClient.__init__` as an invariant:

```python
def __init__(self, ..., pace_seconds: float = 1.0):
    self._pace_seconds = max(pace_seconds, 1.0)
```

**WS-1H** drops the 5 CLI duplicates.

**WS-2A** replaces the mid-fetch mutation in `roster.py:103` with `with_pace` context manager
(RF-SMELL-001) — do not edit `roster.py:103` in this workstream.

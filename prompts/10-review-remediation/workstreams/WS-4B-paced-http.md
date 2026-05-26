# WS-4B — Pacing in PacedHttpClient
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-4b-paced-http` |
| Base | `feature/review-remediation` (after WS-3) |
| Model | claude-sonnet-4-6 |
| Wave | 4a |
| Exec | parallel (with WS-4A) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/http_transport.py`
- Pacing removal in `civix.py:140-159`, `session.py:117-122, 138, 154`

### Forbidden (MUST NOT touch)

- `cli/` package split (WS-4C)
- `sources.py` (WS-4A)

### Close issues

- RF-DRY-006

### On conflict

Rebase after WS-2A if `session.py` public API changed.

## Verification subset

```bash
uv run pytest tests/unit/test_http_transport.py tests/unit/test_civix.py tests/unit/test_legacy.py -q
```

## Mechanical spec (from v1.0.0)

### RF-DRY-006 — Pacing belongs in `PacedHttpClient`

**Files:** `src/texas_turnout_scraper/http_transport.py`; sweep `civix.py:140-159`,
`session.py:117-122, 138, 154`.

Both `CivixClient._get` and `LegacySession._pace`/`_post_form` reimplement pacing on top
of the shared `PacedHttpClient`. Move pacing into `PacedHttpClient` so neither wrapper
needs `time.monotonic()` bookkeeping. Wire `pace_seconds` through the constructor.

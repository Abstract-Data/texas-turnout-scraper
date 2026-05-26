# WS-5A — HTTP symmetric retry
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5a-http-retry` |
| Base | `feature/review-remediation` (after WS-4C) |
| Model | claude-sonnet-4-6 |
| Wave | 5 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/http_transport.py`
- `tests/unit/test_http_transport.py`

### Forbidden (MUST NOT touch)

- `civix.py`, `session.py` (unless import-only)

### Close issues

- P2-ARCH-001

## Verification subset

```bash
uv run pytest tests/unit/test_http_transport.py -v
```

## Mechanical spec (from v1.0.0)

### P2-ARCH-001 — Symmetric retry for `.get` and `.post`

**File:** `src/texas_turnout_scraper/http_transport.py:103-149`

`.get()` retries on 502/503/504 and calls `raise_for_status`. `.post()` does neither —
`LegacySession._post_form` calls `.post()` then `.raise_for_status()` after the fact, so
a 502 in the legacy roster loop fails immediately rather than retrying.

Hoist the retry loop into a shared `_request_with_retry(method, path, **kwargs)` used by
both `.get()` and `.post()`. Add jitter:

```python
time.sleep((2 ** attempt) + random.uniform(0, 0.5))
```

Add a test that asserts the 2-retry-then-fail behavior for both methods.

# WS-2A — LegacySession encapsulation
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-2a-session` |
| Base | `feature/review-remediation` (after Wave 1 gate) |
| Model | claude-sonnet-4-6 |
| Wave | 2 |
| Exec | parallel (must merge before WS-2B) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/session.py`
- `src/texas_turnout_scraper/roster.py`
- `src/texas_turnout_scraper/turnout.py`
- `src/texas_turnout_scraper/elections.py`
- `tests/unit/test_legacy*.py`

### Forbidden (MUST NOT touch)

- `cli.py`
- `civix.py` `_parse_county_csv` paths (WS-2B)
- `models.py` `from_csv_row` (WS-2B)

### Close issues

- RF-SMELL-001

### On conflict

WS-2B must rebase after 2A merges; never parallel-edit `roster.py` with 2B.

## Verification subset

```bash
uv run pytest tests/unit/test_legacy.py -v
uv run ty check
```

## Mechanical spec (from v1.0.0)

### RF-SMELL-001 — Promote `LegacySession` private members to public surface

**Files:** `src/texas_turnout_scraper/session.py`; sweep `roster.py`, `turnout.py`,
`elections.py`, `tests/unit/`.

External modules import `LegacySession` and call `_post_form`, `_pace`, `_pace_seconds`,
`_client`, `_last_request_at`, `_election_details_html` — and `roster.py:103` **mutates**
`_pace_seconds`. This is the structural root cause behind several other issues.

Steps:

1. Rename `_post_form` → `post_form`, `_pace` → `pace`, `_election_details_html` →
   `cached_election_html` (or wrap as a property).
2. Add a public `stream(url, **kwargs)` method that delegates to `self._client.stream(...)`.
3. Replace `pace_seconds` mid-fetch mutation in `roster.py:103` with a context manager:

   ```python
   @contextmanager
   def with_pace(self, pace_seconds: float):
       prior = self._pace_seconds
       self._pace_seconds = max(pace_seconds, 1.0)
       try:
           yield
       finally:
           self._pace_seconds = prior
   ```

4. Update all callers in `roster.py`, `turnout.py`, `elections.py` to use the public API.
5. Sweep `tests/unit/` for tests that mock or assert on underscore names — they'll need
   updating too.

After this, `LegacySession`'s public surface is the only thing external callers depend on,
and future HTTP-backend swaps (async httpx, anyio) become possible.

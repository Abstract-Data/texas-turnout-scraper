# WS-5G — Turnout ColumnDetector
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5g-turnout-parser` |
| Base | `feature/review-remediation` (after WS-4C) |
| Model | claude-sonnet-4-6 |
| Wave | 5 |
| Exec | parallel (must merge before WS-5H) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/turnout.py` (`_parse_turnout_html`, `_detect_column_map`, ColumnDetector)

### Forbidden (MUST NOT touch)

- `elections.py` (WS-5H)

### Close issues

- RF-CPLX-002

## Verification subset

```bash
uv run pytest tests/unit/test_legacy.py -q -k turnout
```

## Mechanical spec (from v1.0.0)

### RF-CPLX-002 — Extract a `ColumnDetector` for turnout HTML

**File:** `src/texas_turnout_scraper/turnout.py:185-300`

`_parse_turnout_html` + `_detect_column_map` carry 4 nested branches and 3 heuristic
strategies. Extract a `ColumnDetector` class (or a small table-driven approach) so
adding a new column-name variant doesn't require touching the parser.

**WS-5H** follows on the same file for BeautifulSoup narrowing — merge 5G before 5H.

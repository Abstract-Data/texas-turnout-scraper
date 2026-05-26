# WS-2D — Source enum sweep
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-2d-source-enum` |
| Base | `feature/review-remediation` (after WS-1D merged) |
| Model | claude-haiku-4-5 |
| Wave | 2 |
| Exec | parallel[after: 1D] |

### File lock (MAY edit)

- `src/texas_turnout_scraper/enums.py` (add `Source`)
- Sweep: `cli.py`, `mcp_server.py`, `writer.py`, `audit.py` for `"civix"` / `"legacy"` literals

### Forbidden (MUST NOT touch)

- `sources.py` (WS-4A)
- `cli.py` structural refactor (WS-4C)

### Close issues

- RF-SMELL-003

### On conflict

Stop on `cli.py` if WS-1H not merged; rebase after Wave 1 gate.

## Verification subset

```bash
uv run pytest tests/unit -q
uv run ty check
grep -rn '"civix"\|"legacy"' src/  # expect enum members or str(Source.*) only
```

## Mechanical spec (from v1.0.0)

### RF-SMELL-003 — Introduce `Source` enum

**Files:** `src/texas_turnout_scraper/enums.py`; sweep `cli.py`, `mcp_server.py`,
`writer.py`, `audit.py`.

The string literals `"civix"` and `"legacy"` appear ~25 times. Promote to:

```python
class Source(str, Enum):
    CIVIX = "civix"
    LEGACY = "legacy"
```

Keep the string serialization (subclassing `str`) so existing JSON / CSV consumers keep
working.

### Phase 2 verification (coordinator after 2A→2B→2C+2D)

```bash
uv run ruff check . --fix && uv run ruff format .
uv run ty check
uv run pytest tests/unit -q
uv run pytest tests/verify -q
```

Tests in `tests/unit/test_legacy.py`, `test_civix.py`, `test_writer.py`,
`test_voterfile.py` must all stay green.

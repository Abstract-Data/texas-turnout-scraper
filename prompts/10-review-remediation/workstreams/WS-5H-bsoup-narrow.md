# WS-5H — BeautifulSoup Tag narrowing
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5h-bsoup-narrow` |
| Base | `feature/review-remediation` (after WS-5G merged) |
| Model | claude-haiku-4-5 |
| Wave | 5 |
| Exec | sequential[after: 5G] on `turnout.py` |

### File lock (MAY edit)

- `src/texas_turnout_scraper/turnout.py` (type-ignore cleanup)
- `src/texas_turnout_scraper/elections.py`

### Forbidden (MUST NOT touch)

- ColumnDetector structure from WS-5G beyond integration points

### Close issues

- RF-SMELL-007

## Verification subset

```bash
uv run ty check
uv run pytest tests/unit/test_legacy.py -q
```

**Final gate (coordinator after all Wave 5 merges):**

```bash
uv run ruff check --select=PLR0915,PLR0912,C901
uv run ruff check . --fix && uv run ruff format .
uv run ty check
uv run pytest tests/unit -q
uv run pytest tests/verify -q
```

## Mechanical spec (from v1.0.0)

### RF-SMELL-007 — Tighten BeautifulSoup `# type: ignore` comments

**Files:** `turnout.py:130, 142, 158`, `elections.py:81, 137`

Local helpers that narrow `Tag` early (`def _as_tag(node) -> Tag: assert isinstance(node, Tag); return node`)
let the rest of the code be `# type: ignore`-free.

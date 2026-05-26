# WS-1D — Delete dead PoliticalParty enum
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1d-enums-dead` |
| Base | `feature/review-remediation` |
| Model | claude-haiku-4-5 |
| Wave | 1 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/enums.py`
- `src/texas_turnout_scraper/__init__.py`
- `src/texas_turnout_scraper/models.py` — remove `PoliticalParty` import only

### Forbidden (MUST NOT touch)

- `cli.py`
- `Source` enum (WS-2D)

### Close issues

- RF-DEAD-001

### On conflict

Stop; WS-2D depends on this merging before `Source` enum work.

## Verification subset

```bash
uv run ty check
uv run pytest tests/unit -q -k "not integration"
grep -r PoliticalParty src/ tests/  # expect no matches
```

## Mechanical spec (from v1.0.0)

### RF-DEAD-001 — Delete unused `PoliticalParty` enum

**Files:** `src/texas_turnout_scraper/enums.py:30-40`, `src/texas_turnout_scraper/__init__.py`

`PoliticalParty` is defined and exported but never referenced in `src/`, `tests/`, or
`mcp_server.py`. Delete the enum and its export. If the voterfile party column is
eventually wired up, re-introduce the enum then.

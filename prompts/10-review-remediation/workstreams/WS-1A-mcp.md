# WS-1A — MCP fixes + contract tests
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1a-mcp` |
| Base | `feature/review-remediation` |
| Model | claude-sonnet-4-6 |
| Wave | 1 |
| Exec | parallel (skip if WS-0 merged) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/mcp_server.py`
- `tests/unit/test_mcp_server.py` (new — **exclusive owner** of MCP contract tests)

### Forbidden (MUST NOT touch)

- `cli.py`
- All other `src/` files

### Close issues

- P1-ARCH-001
- RF-SMELL-002

### On conflict

Stop if `test_mcp_server.py` conflicts with WS-0; coordinator keeps one version.

## Verification subset

```bash
uv run pytest tests/unit/test_mcp_server.py -v
uv run ty check
```

## Mechanical spec (from v1.0.0)

### P1-ARCH-001 — Fix broken MCP server calls (RF-SMELL-002)

**Files:** `src/texas_turnout_scraper/mcp_server.py`

Three call sites will raise on first invocation. Fix all three:

```python
# mcp_server.py:100  (was: ev_date=ev_date)
rows = client.fetch_ev_turnout(election_id=election_id, election_date=ev_date)

# mcp_server.py:148  (was: client.fetch_county_roster(...))
# fetch_county_roster is a MODULE-LEVEL function, not a method
from .civix import fetch_county_roster
roster = fetch_county_roster(
    client,
    election_id=election_id,
    election_date=ev_date,
    county_name=county_name,
    county_id=county_id,
)

# mcp_server.py:194  (was: ed_date=ed_date)
rows = client.fetch_ed_turnout(election_id=election_id, election_date=ed_date)
```

Verify against actual signatures at `src/texas_turnout_scraper/civix.py:217-220`, `:414-417`,
`:557-563`.

**Also (RF-SMELL-002 contract test):** add `tests/unit/test_mcp_server.py` with one test
per `@mcp.tool()` that invokes the function against respx-mocked Civix endpoints and asserts
the returned dict shape. This is the test that would have caught the bug at PR time.

**Dispatch example:**

> Fix P1-ARCH-001 in mcp_server.py per spec; add tests/unit/test_mcp_server.py with respx mocks for every @mcp.tool. Do not edit cli.py.

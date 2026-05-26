# Skill: mcp-tool-testing

Use when adding or changing an `@mcp.tool()` in `src/texas_turnout_scraper/mcp_server.py`.

Every MCP tool MUST have a behavioral test in `tests/unit/test_mcp_server.py` that:
1. Mocks the underlying HTTP call (respx for httpx, or appropriate adapter for cloudscraper).
2. Invokes the tool function directly (not via the MCP transport — that's integration territory).
3. Asserts the returned dict shape AND at least one field value.

## Canonical template

```python
import pytest
import respx
from httpx import Response
from texas_turnout_scraper.mcp_server import <your_tool>


@pytest.mark.asyncio
async def test_<your_tool>_happy_path(respx_mock):
    # 1. Mock the wire response
    respx_mock.get("https://goelect.txelections.civixapps.com/...").mock(
        return_value=Response(200, json={"upload": "<base64-encoded-csv>"})
    )

    # 2. Invoke the tool the same way the MCP server invokes it (NOT the underlying client)
    result = await <your_tool>(election_id="58315", ev_date="2026-10-21")

    # 3. Assert shape AND at least one field
    assert "records" in result
    assert isinstance(result["records"], list)
    assert result["election_id"] == "58315"


@pytest.mark.asyncio
async def test_<your_tool>_bad_input(respx_mock):
    result = await <your_tool>(election_id="not-a-number", ev_date="garbage")
    assert "error" in result  # MCP tools return {"error": ...} dicts, not raise
```

## What this protects against

The 2026-05-25 review's P1-ARCH-001 was three `@mcp.tool()` functions calling Civix client
methods with wrong keyword argument names (`ev_date=` instead of `election_date=`). They had
zero behavioral tests — only an import-existence check in `test_cli_legacy.py:68`. The bugs
would have raised `TypeError` on first invocation in production. This skill exists so that
class of bug is caught at PR time, not by an MCP user.

## Verify suite enforces this

`tests/verify/check_mcp_tools_have_tests.py` parses `mcp_server.py` for `@mcp.tool()`
decorators and fails the build if any decorated function lacks a test function whose name
contains the tool name. So you literally cannot ship a new tool without a test.

## When to invoke the contract checker

After writing the test, ask the `mcp-contract-checker` subagent to verify the call site:

> "Run mcp-contract-checker on src/texas_turnout_scraper/mcp_server.py"

It compares every `@mcp.tool()`'s call signature against the actual client method signature
in `civix.py` / `legacy_api.py`. This catches the second class of MCP bug: the test exists
but the call site has the wrong kwargs (which the test would have caught if it weren't
mocking too aggressively).

## References

- AGENTS.md → `## Anti-Pattern Warnings`
- `tests/verify/check_mcp_tools_have_tests.py`
- `.claude/agents/mcp-contract-checker.md`
- `.claude/hooks/mcp-tool-test-required.sh` (PostToolUse reminder)

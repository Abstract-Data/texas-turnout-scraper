"""Verify that every @mcp.tool() in src/texas_turnout_scraper/mcp_server.py
has a corresponding test function in tests/unit/test_mcp_server.py.

This is the guardrail that would have caught the 5/25 review's P1-ARCH-001
(three broken MCP tool integrations) at CI time. Without this, MCP tools
can ship with zero behavioral coverage.

How it works:
- Parse mcp_server.py for @mcp.tool() decorators
- For each decorated function, look for a corresponding test function
- A "corresponding test" is any test function whose name contains the tool name
- Missing test → assertion failure (with the list of tools without tests)

Run via:
    uv run pytest tests/verify/check_mcp_tools_have_tests.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_SERVER = REPO_ROOT / "src" / "texas_turnout_scraper" / "mcp_server.py"
TEST_FILE = REPO_ROOT / "tests" / "unit" / "test_mcp_server.py"


def _collect_mcp_tools(source_path: Path) -> list[str]:
    """Return names of functions decorated with @mcp.tool() (or @mcp.tool)."""
    if not source_path.exists():
        return []
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    tools: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            # @mcp.tool() — Call with attribute func
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if dec.func.attr == "tool":
                    tools.append(node.name)
                    break
            # @mcp.tool — bare attribute
            elif isinstance(dec, ast.Attribute) and dec.attr == "tool":
                tools.append(node.name)
                break
    return tools


def _collect_test_function_names(source_path: Path) -> list[str]:
    """Return all test function names from the test file."""
    if not source_path.exists():
        return []
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    ]


def check_mcp_tools_have_tests() -> None:
    """Fail if any @mcp.tool() lacks a corresponding test_<name>* function.

    Naming convention: a test function "covers" a tool if its name contains the
    tool name as a substring (so `list_elections` is covered by `test_list_elections`,
    `test_list_elections_empty`, `test_list_elections_filters_by_year`, etc.)
    """
    tools = _collect_mcp_tools(MCP_SERVER)
    assert tools, (
        f"No @mcp.tool() decorators found in {MCP_SERVER.relative_to(REPO_ROOT)} — "
        f"either the file is wrong or the decorator pattern changed."
    )

    test_names = _collect_test_function_names(TEST_FILE)
    missing: list[str] = []
    for tool in tools:
        # A test covers the tool if its name contains the tool name as a token
        if not any(tool in t for t in test_names):
            missing.append(tool)

    if missing:
        lines = [
            "",
            "The following @mcp.tool() functions in mcp_server.py have NO corresponding test",
            "in tests/unit/test_mcp_server.py:",
            "",
            *(f"  - {name}" for name in missing),
            "",
            "Every MCP tool MUST have at least one behavioral test (respx-mocked HTTP, then",
            "invoke the tool, then assert the returned dict shape). Without this, broken",
            "keyword arguments or wrong client methods will not be caught until the first",
            "AI-agent invocation fails in production.",
            "",
            "Reference: the 5/25 review's P1-ARCH-001 finding (mcp_server.py:100, 148, 194).",
        ]
        raise AssertionError("\n".join(lines))


def test_every_mcp_tool_has_a_test() -> None:
    """pytest entry point — wraps check_mcp_tools_have_tests."""
    check_mcp_tools_have_tests()


if __name__ == "__main__":
    check_mcp_tools_have_tests()
    print(f"OK — all MCP tools in {MCP_SERVER.name} have at least one test.")

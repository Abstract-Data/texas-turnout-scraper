#!/usr/bin/env bash
# mcp-tool-test-required.sh — PostToolUse hook (Write|Edit)
#
# Warns (does NOT block) when src/texas_turnout_scraper/mcp_server.py is edited
# without a corresponding edit to tests/unit/test_mcp_server.py. This is the
# soft-touch guardrail that pairs with the hard CI check in
# tests/verify/check_mcp_tools_have_tests.py.
#
# Why warn and not block: the test file may already exist and only need updating
# on a future commit. The pairing matters at PR time (where CI runs), not at
# every keystroke. The warning is here to remind the author in the moment.
#
# Reference: 5/25 review P1-ARCH-001 — three MCP tools shipped with wrong kwargs
# because no test invoked them.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

# Only fire on Write/Edit of mcp_server.py
if [[ "$TOOL" != "Write" && "$TOOL" != "Edit" ]]; then
  exit 0
fi
if [[ "$FILE" != *"src/texas_turnout_scraper/mcp_server.py" ]]; then
  exit 0
fi

cat >&2 <<'MSG'
⚠️  REMINDER: You just edited mcp_server.py.
   Every @mcp.tool() must have a corresponding test in tests/unit/test_mcp_server.py.

   The CI check tests/verify/check_mcp_tools_have_tests.py will FAIL the build
   if any tool lacks a test. The 5/25 review's P1-ARCH-001 (three broken MCP
   tools shipping with wrong keyword args) is exactly what this guards against.

   Quick template:
       async def test_<your_tool_name>(respx_mock):
           respx_mock.get("https://...").mock(return_value=Response(...))
           result = await <your_tool_name>(...)
           assert result == {...}

   See .claude/skills/mcp-tool-testing.md for the full pattern.
MSG

exit 0

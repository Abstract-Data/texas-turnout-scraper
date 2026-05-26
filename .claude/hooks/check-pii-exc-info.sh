#!/usr/bin/env bash
# check-pii-exc-info.sh — PostToolUse hook (Write|Edit)
#
# Blocks edits that introduce `exc_info=True` in files that touch PII fields
# (VOTER_NAME or ID_VOTER). The 5/25 review's P1-SEC-001 was a bare
# `except Exception:` with `exc_info=True` in roster.py:319 — a malformed CSV
# row would have written voter names into the log via the traceback.
#
# The rule: in any module that reads or constructs voter PII, exception logging
# must NOT include the traceback. Use the exception class name only.
#
# This complements the ruff `BLE` selector (which catches the bare-except) and
# the AGENTS.md NEVER DO rule (which states the principle).

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

# Only gate Write/Edit on .py files
if [[ "$TOOL" != "Write" && "$TOOL" != "Edit" ]]; then
  exit 0
fi
if [[ "$FILE" != *.py ]]; then
  exit 0
fi

# Skip tests — test fixtures legitimately have tracebacks
case "$FILE" in
  */tests/*) exit 0 ;;
esac

# Only check files that touch PII (VOTER_NAME / ID_VOTER literals)
if ! grep -qE 'VOTER_NAME|ID_VOTER|voter_name|id_voter' "$FILE" 2>/dev/null; then
  exit 0
fi

# Look for exc_info=True in the file
if grep -nE 'exc_info\s*=\s*True' "$FILE" > /tmp/_pii_exc_hits.txt 2>/dev/null; then
  cat >&2 <<MSG
{"decision":"block","reason":"BLOCK: exc_info=True in a PII-touching file ($FILE).\nTraceback content may include voter names or VUIDs from row values. AGENTS.md NEVER DO rule.\nLine(s):\n$(cat /tmp/_pii_exc_hits.txt)\n\nFix: log the exception class name instead, e.g.\n    except (csv.Error, ValueError) as exc:\n        logger.warning('Parse failed for county=%s — %s', county_id, type(exc).__name__)"}
MSG
  rm -f /tmp/_pii_exc_hits.txt
  exit 2
fi

rm -f /tmp/_pii_exc_hits.txt
exit 0

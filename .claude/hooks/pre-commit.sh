#!/usr/bin/env bash
# pre-commit.sh — Pre-tool hook: run fast lint checks before committing
# Fires on Bash commands that look like git commit. Runs ruff check + ruff format --check.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

if [[ "$TOOL" == "Bash" ]] && echo "$COMMAND" | grep -qE 'git\s+commit'; then
  cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

  echo "--- pre-commit: ruff check ---"
  if ! ruff check . --select E,W,F --quiet 2>&1; then
    echo '{"decision":"block","reason":"Blocked: ruff check found errors. Run: ruff check . --fix"}' >&2
    exit 2
  fi

  echo "--- pre-commit: ruff format --check ---"
  if ! ruff format . --check --quiet 2>&1; then
    echo '{"decision":"block","reason":"Blocked: ruff format check failed. Run: ruff format ."}' >&2
    exit 2
  fi

  echo "--- pre-commit: passed ---"
fi

exit 0

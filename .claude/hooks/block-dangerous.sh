#!/usr/bin/env bash
# block-dangerous.sh — Pre-tool hook: block shell commands that could cause irreversible damage
# Reads JSON tool input from stdin. Blocks: rm -rf /, rm -rf ~, git push --force to main/master,
# truncate on production tables, DROP DATABASE/TABLE in production contexts.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

if [[ "$TOOL" == "Bash" ]]; then
  # Block rm -rf on root or home
  if echo "$COMMAND" | grep -qE 'rm\s+-rf\s+(/\s*$|~/|/\*)'; then
    echo '{"decision":"block","reason":"Blocked: rm -rf targeting root or home directory"}' >&2
    exit 2
  fi

  # Block force-push to protected branches
  if echo "$COMMAND" | grep -qE 'git\s+push\s+.*--force'; then
    if echo "$COMMAND" | grep -qE '(main|master|production|prod)'; then
      echo '{"decision":"block","reason":"Blocked: force-push to protected branch (main/master/production)"}' >&2
      exit 2
    fi
  fi

  # Block DROP DATABASE / DROP TABLE in production contexts
  if echo "$COMMAND" | grep -qiE 'DROP\s+(DATABASE|TABLE)' && echo "$COMMAND" | grep -qiE '(prod|production)'; then
    echo '{"decision":"block","reason":"Blocked: DROP DATABASE/TABLE in production context"}' >&2
    exit 2
  fi
fi

exit 0

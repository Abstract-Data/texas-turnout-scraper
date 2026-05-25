#!/usr/bin/env bash
# block-env-writes.sh — Pre-tool hook: prevent writes to .env files that could expose secrets
# Blocks Write/Edit operations targeting .env, .env.local, .env.production, etc.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // ""')

if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" ]]; then
  if echo "$FILE" | grep -qE '(^|/)\.env(\.[^/]*)?$'; then
    echo '{"decision":"block","reason":"Blocked: direct write to .env file. Edit .env.example instead and let the developer copy it."}' >&2
    exit 2
  fi
fi

exit 0

#!/usr/bin/env bash
# block-raw-git.sh — PreToolUse hook
#
# This project uses GitButler for virtual branch management. Raw `git checkout -b`,
# `git branch`, and `git merge` bypass GitButler's tracking. This hook blocks those
# subcommands. Read-only `git` commands are not affected.
#
# Bypass for native git workflows (release tagging, etc): set GITBUTLER_BYPASS=1.
# See docs/GITBUTLER.md for the canonical `gb` CLI reference.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)

if [[ "$TOOL" != "Bash" ]]; then
  exit 0
fi

if [[ "${GITBUTLER_BYPASS:-0}" == "1" ]]; then
  exit 0
fi

# Block: git checkout -b, git branch <args>, git merge <args>
# Allow:  git status / diff / log / show / blame / pull / fetch / push / commit / stash / restore
if echo "$COMMAND" | grep -qE '(^|[[:space:];&|])git[[:space:]]+(checkout[[:space:]]+-b|branch([[:space:]]|$)|merge([[:space:]]|$))'; then
  cat >&2 <<'MSG'
{"decision":"block","reason":"BLOCK: raw git branch/merge — this project uses GitButler. Use the gb CLI:\n  gb branch create <name>     # create virtual branch\n  gb branch list              # see active virtuals\n  gb branch apply <name>      # switch\nSee docs/GITBUTLER.md. Set GITBUTLER_BYPASS=1 to override for release tagging."}
MSG
  exit 2
fi

exit 0

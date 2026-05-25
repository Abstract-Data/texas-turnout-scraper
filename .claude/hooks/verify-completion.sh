#!/usr/bin/env bash
# verify-completion.sh — Stop hook: three-gate completion check
# Gate 1: All TASK.md checkboxes checked
# Gate 2: No failing unit tests
# Gate 3: No ruff lint errors
# Blocks the Stop event if any gate fails.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

FAIL=0

# Gate 1: TASK.md open checkboxes
if [[ -f TASK.md ]]; then
  OPEN=$(grep -c '^\- \[ \]' TASK.md 2>/dev/null || true)
  if [[ "$OPEN" -gt 0 ]]; then
    echo "❌ Gate 1 FAILED: $OPEN unchecked task(s) in TASK.md" >&2
    FAIL=1
  else
    echo "✅ Gate 1: TASK.md — all boxes checked"
  fi
else
  echo "⚠️  Gate 1: TASK.md not found — skipping"
fi

# Gate 2: Unit tests
if command -v pytest &>/dev/null; then
  if ! pytest tests/unit -q --tb=no 2>&1 | tail -5; then
    echo "❌ Gate 2 FAILED: unit tests are failing" >&2
    FAIL=1
  else
    echo "✅ Gate 2: unit tests passing"
  fi
else
  echo "⚠️  Gate 2: pytest not found — skipping"
fi

# Gate 3: Lint
if command -v ruff &>/dev/null; then
  if ! ruff check . --select E,W,F --quiet 2>&1; then
    echo "❌ Gate 3 FAILED: ruff lint errors present" >&2
    FAIL=1
  else
    echo "✅ Gate 3: ruff lint clean"
  fi
else
  echo "⚠️  Gate 3: ruff not found — skipping"
fi

if [[ "$FAIL" -eq 1 ]]; then
  echo '{"decision":"block","reason":"Completion gates failed — see output above"}' >&2
  exit 2
fi

exit 0

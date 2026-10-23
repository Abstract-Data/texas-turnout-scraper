#!/usr/bin/env bash
# Setup script for the "Texas Turnout Scraper" Claude Code cloud environment.
# Paste this file's contents into the environment's "Setup script" field.
# Idempotent: safe to re-run. Never prints secret values.
#
# Provides: uv + locked project deps (all extras), ruff on PATH (the pre-commit hook
# calls bare `ruff`), 1Password CLI (op run --environment needs >= 2.41.0), jq (hooks
# parse their input with it), and a warm npm cache for the pinned Context7 MCP server.

set -euo pipefail

UV_VERSION="0.13.4"
OP_VERSION="2.41.0"
OP_SHA256_AMD64="498bbaa154f0d90749325a14e04488fcd84930a426ae92ebf421d2a7d793a80a"
OP_SHA256_ARM64="bc4ac99201909f96f67e7a89601dbf5388f991aca53120db6fc3f1666dff706b"
CONTEXT7_VERSION="4.4.4"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

echo "--- system packages ---"
missing=()
for tool in jq unzip curl; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq --no-install-recommends "${missing[@]}"
fi

echo "--- uv ${UV_VERSION} ---"
if ! command -v uv >/dev/null 2>&1 || [ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]; then
  pip install -q "uv==${UV_VERSION}" 2>/dev/null \
    || pip install -q --break-system-packages "uv==${UV_VERSION}"
fi
uv --version

echo "--- 1Password CLI ${OP_VERSION} ---"
if ! command -v op >/dev/null 2>&1 || [ "$(op --version)" != "$OP_VERSION" ]; then
  case "$(uname -m)" in
    x86_64) op_arch="amd64"; op_sha="$OP_SHA256_AMD64" ;;
    aarch64 | arm64) op_arch="arm64"; op_sha="$OP_SHA256_ARM64" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  zip="op_linux_${op_arch}_v${OP_VERSION}.zip"
  curl -sSfL -o "$tmp/$zip" "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/${zip}"
  echo "${op_sha}  $tmp/$zip" | sha256sum -c --quiet -
  unzip -q -o "$tmp/$zip" op -d "$tmp"
  $SUDO install -m 0755 "$tmp/op" /usr/local/bin/op
fi
op --version

echo "--- project dependencies ---"
repo="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
if [ -f "$repo/pyproject.toml" ]; then
  cd "$repo"
  # Same flags as .github/workflows/ci.yml so cloud and CI resolve identically.
  uv sync --locked --dev --extra cli --extra waf --extra voterfile --extra mcp
  # .claude/hooks/pre-commit.sh runs bare `ruff`; expose the project's pinned copy.
  $SUDO ln -sf "$repo/.venv/bin/ruff" /usr/local/bin/ruff
else
  echo "pyproject.toml not found under $repo; skipping dependency install (run 'uv sync --locked --dev --extra cli --extra waf --extra voterfile --extra mcp' in the repo)."
fi

echo "--- Context7 MCP (npm cache) ---"
if command -v npm >/dev/null 2>&1; then
  npm cache add "@upstash/context7-mcp@${CONTEXT7_VERSION}" >/dev/null 2>&1 \
    || echo "npm cache warm failed (non-fatal); npx will fetch on first use."
fi

echo "--- 1Password access check (names and status only) ---"
for var in OP_SERVICE_ACCOUNT_TOKEN OP_ENVIRONMENT_ID; do
  if [ -n "${!var:-}" ]; then echo "$var: set"; else echo "$var: MISSING"; fi
done
if [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] && [ -n "${OP_ENVIRONMENT_ID:-}" ]; then
  if op run --environment "$OP_ENVIRONMENT_ID" -- true >/dev/null 2>&1; then
    echo "op run --environment: OK"
  else
    echo "op run --environment: FAILED (check token scope and environment id; non-fatal)"
  fi
fi

echo "setup complete"

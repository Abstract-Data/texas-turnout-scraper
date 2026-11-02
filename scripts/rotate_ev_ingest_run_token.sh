#!/usr/bin/env bash
# Rotate the ev-ingest Worker's RUN_TOKEN. The 1Password item below is the token's source of truth.
# The new token is staged in the item's `pending` field, set on the Worker, and only then promoted
# to `credential`, so `credential` keeps matching the Worker until the Worker has the new token.
# The token never reaches stdout or any argv: `op` reads it from a private template file removed on
# exit, `wrangler` and `curl` from stdin.
#   bash scripts/rotate_ev_ingest_run_token.sh            # rotate only
#   bash scripts/rotate_ev_ingest_run_token.sh 10/24/2026 # rotate, then dry-run /spike for that day
set -euo pipefail

VAULT="${TX_TURNOUT_OP_VAULT:-qxyxd4oqdy5mpmg4t4qh3bfflm}"
TITLE="tx-turnout-ev-ingest-preview RUN_TOKEN"
URL="https://tx-turnout-ev-ingest-preview.johnreakin.workers.dev"

cd "$(git rev-parse --show-toplevel)/infra/ev_ingest"
umask 077
template="$(mktemp)"
trap 'rm -f "$template" "$template.new"' EXIT
token="$(openssl rand -hex 32)"

# Apply a jq filter to the template; the filter reads the token from the environment, not argv.
apply_to_template() {
  NEW_TOKEN="$token" jq "$1" "$template" >"$template.new" && mv "$template.new" "$template"
}

stage='.fields += [{id: "pending", label: "pending", type: "CONCEALED", value: env.NEW_TOKEN}]'
if op item get "$TITLE" --vault "$VAULT" --format json >"$template" 2>/dev/null; then
  if jq -e '.fields | any(.label == "pending")' "$template" >/dev/null; then
    cat >&2 <<EOF
"$TITLE" already has a pending token from an unfinished rotation, which may be the one the Worker
holds. Try it against GET $URL/status. If it works, copy it to credential. Then delete pending
(op item edit "$TITLE" --vault "$VAULT" "pending[delete]") and rerun.
EOF
    exit 1
  fi
  apply_to_template "$stage"
  op item edit "$TITLE" --vault "$VAULT" --template="$template" >/dev/null
else
  op item template get "API Credential" >"$template"
  apply_to_template "$stage"
  op item create --title "$TITLE" --vault "$VAULT" --template="$template" >/dev/null
fi

if ! printf '%s' "$token" | npx wrangler secret put RUN_TOKEN; then
  echo "Worker update failed: credential is unchanged; pending holds the unused new token." >&2
  echo "Check GET $URL/status with each value, as a failed put may still have applied." >&2
  exit 1
fi

if ! {
  op item get "$TITLE" --vault "$VAULT" --format json >"$template" &&
    apply_to_template 'if any(.fields[]; .id == "credential") then (.fields[] | select(.id == "credential") | .value) = env.NEW_TOKEN else error("no credential field") end' &&
    op item edit "$TITLE" --vault "$VAULT" --template="$template" "pending[delete]" >/dev/null
}; then
  echo "The Worker has the new token, but promotion failed: copy pending to credential." >&2
  exit 1
fi
echo "RUN_TOKEN set on the Worker and promoted in 1Password ($TITLE)"

if [[ $# -ge 1 ]]; then
  body="$(printf 'Authorization: Bearer %s\n' "$token" |
    curl -sS --fail-with-body -X POST "$URL/spike?date=$1" -H @-)" || {
    printf '%s\n' "${body:-}"
    echo "/spike request failed" >&2
    exit 1
  }
  printf '%s\n' "$body"
  # /spike answers 200 even when the dry run fails, so the exit code is in the body.
  jq -e '.exit_code == 0' <<<"$body" >/dev/null || {
    echo "/spike dry run did not report exit_code 0" >&2
    exit 1
  }
fi

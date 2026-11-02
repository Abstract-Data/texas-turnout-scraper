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

# A lookup failure must stop here. Treating it as "no item" would skip the pending check below.
matches="$(op item list --vault "$VAULT" --format json |
  jq --arg title "$TITLE" '[.[] | select(.title == $title)] | length')"
case "$matches" in
  0) op item template get "API Credential" >"$template" ;;
  1)
    # --reveal: a masked credential written back through the template would overwrite the real one.
    op item get "$TITLE" --vault "$VAULT" --reveal --format json >"$template"
    if jq -e '.fields | any(.label == "pending")' "$template" >/dev/null; then
      cat >&2 <<EOF
"$TITLE" already has a pending token from an unfinished rotation, which may be the one the Worker
holds. Try it against GET $URL/status. If it works, copy it to credential. Then delete pending
(op item edit "$TITLE" --vault "$VAULT" "pending[delete]") and rerun.
EOF
      exit 1
    fi
    ;;
  *)
    echo "Found $matches items titled \"$TITLE\" in the vault; expected at most one." >&2
    exit 1
    ;;
esac

# Checked here, before the Worker changes, so promotion cannot be the first to find it missing.
apply_to_template 'if any(.fields[]; .id == "credential")
  then .fields += [{id: "pending", label: "pending", type: "CONCEALED", value: env.NEW_TOKEN}]
  else error("the item has no credential field") end'
if [[ "$matches" == 0 ]]; then
  op item create --title "$TITLE" --vault "$VAULT" --template="$template" >/dev/null
else
  op item edit "$TITLE" --vault "$VAULT" --template="$template" >/dev/null
fi

if ! printf '%s' "$token" | npx wrangler secret put RUN_TOKEN; then
  echo "Worker update failed: credential is unchanged; pending holds the unused new token." >&2
  echo "Check GET $URL/status with each value, as a failed put may still have applied." >&2
  exit 1
fi

if ! {
  op item get "$TITLE" --vault "$VAULT" --reveal --format json >"$template" &&
    apply_to_template '(.fields[] | select(.id == "credential") | .value) = env.NEW_TOKEN' &&
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

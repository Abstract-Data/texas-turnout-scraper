#!/usr/bin/env bash
# Rotate the ev-ingest Worker's RUN_TOKEN: store it in 1Password first, then set it on the Worker.
# The token never reaches stdout or argv of anything but `op` (which writes it to the vault).
#   bash scripts/rotate_ev_ingest_run_token.sh            # rotate only
#   bash scripts/rotate_ev_ingest_run_token.sh 10/24/2026 # rotate, then dry-run /spike for that day
set -euo pipefail

VAULT="${TX_TURNOUT_OP_VAULT:-qxyxd4oqdy5mpmg4t4qh3bfflm}"
TITLE="tx-turnout-ev-ingest-preview RUN_TOKEN"
URL="https://tx-turnout-ev-ingest-preview.johnreakin.workers.dev"

cd "$(git rev-parse --show-toplevel)/infra/ev_ingest"
token="$(openssl rand -hex 32)"

if op item get "$TITLE" --vault "$VAULT" >/dev/null 2>&1; then
  op item edit "$TITLE" --vault "$VAULT" "credential=$token" >/dev/null
else
  op item create --category "API Credential" --title "$TITLE" --vault "$VAULT" \
    "credential=$token" >/dev/null
fi
printf '%s' "$token" | npx wrangler secret put RUN_TOKEN
echo "RUN_TOKEN stored in 1Password ($TITLE) and set on the Worker"

if [[ $# -ge 1 ]]; then
  curl -sS -X POST "$URL/spike?date=$1" -H "Authorization: Bearer $token"
  echo
fi

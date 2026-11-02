#!/usr/bin/env bash
# Issue an expiring consumer API key with every env var `tx-turnout api generate` needs already set.
#   bash scripts/issue_api_key.sh "Ada Lovelace" ada@example.org             # 90 days, roster scope
#   bash scripts/issue_api_key.sh "Ada Lovelace" ada@example.org 30 aggregates
# Sources, none of which reach stdout or a file:
#   - CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, CLOUDFLARE_KV_NAMESPACE_ID: the 1Password
#     Environment, via `op run`.
#   - CLOUDFLARE_PREVIEW_D1_DATABASE_ID: not in the Environment (it is a Terraform output), so it is
#     looked up by name through the Cloudflare API with the Environment's token.
#   - CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET: the Access pair stored on an already-issued key
#     item. The pair is shared by every key, so any issued item carries it.
# Needs an active `op` session (`op signin`).
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: bash scripts/issue_api_key.sh NAME EMAIL [DAYS=90] [SCOPE=roster]" >&2
  exit 2
fi
name="$1"
email="$2"
days="${3:-90}"
scope="${4:-roster}"

OP_ENV="${TX_TURNOUT_OP_ENV:-s6tljdcafkgqxfuxhcnj3m5x5a}"
VAULT="${TX_TURNOUT_OP_VAULT:-qxyxd4oqdy5mpmg4t4qh3bfflm}"
PREVIEW_D1_NAME="${TX_TURNOUT_PREVIEW_D1_NAME:-tx-turnout-preview}"
# "texas-turnout-scraper: Smoke Test" in the issued-keys vault.
ACCESS_ITEM="${TX_TURNOUT_ACCESS_ITEM:-tdaoofyafrrolklwb54m77vgh4}"

CF_ACCESS_CLIENT_ID="$(op read "op://$VAULT/$ACCESS_ITEM/CF-Access-Client-Id")"
CF_ACCESS_CLIENT_SECRET="$(op read "op://$VAULT/$ACCESS_ITEM/CF-Access-Client-Secret")"
export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET
export CLOUDFLARE_ACCOUNT_ID=e83f342a6c0f6e20b80697363714bf7b

cd "$(git rev-parse --show-toplevel)"
op run --environment "$OP_ENV" -- bash -c '
set -euo pipefail
CLOUDFLARE_PREVIEW_D1_DATABASE_ID="$(
  curl -fsS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/d1/database?name=$1" \
    | python3 -c "import sys,json; n=sys.argv[1]; print(next(r[\"uuid\"] for r in json.load(sys.stdin)[\"result\"] if r[\"name\"]==n))" "$1"
)"
export CLOUDFLARE_PREVIEW_D1_DATABASE_ID
shift
exec uv run tx-turnout api generate "$@"
' _ "$PREVIEW_D1_NAME" --name "$name" --email "$email" --days "$days" --scope "$scope"

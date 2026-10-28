#!/usr/bin/env bash
# Local run of the early-vote loader: the same steps the ingest Container runs
# (scripts/ev_ingest_steps.sh), with secrets from the 1Password Environment. Prints counts only.
#   bash scripts/local_ev_backfill.sh            # backfill election 53815
#   bash scripts/local_ev_backfill.sh dry_run    # hash every day, write nothing
set -euo pipefail

OP_ENV=s6tljdcafkgqxfuxhcnj3m5x5a
export CLOUDFLARE_ACCOUNT_ID=e83f342a6c0f6e20b80697363714bf7b
export EV_SNAPSHOT_BUCKET=tx-turnout-ev-snapshots-preview

cd "$(git rev-parse --show-toplevel)"
uv sync --locked --extra waf --extra loader --quiet
op run --environment "$OP_ENV" -- uv run --no-sync bash scripts/ev_ingest_steps.sh "${1:-apply}"

#!/usr/bin/env bash
# Local run of the early-vote sync apply job (.github/workflows/early-vote-sync.yml), same steps
# and order. Prints counts only. Skips repeat_build, which is paused until after 2026-11-03 (ADR-014).
#   bash scripts/local_ev_backfill.sh            # backfill election 53815
set -euo pipefail

ELECTION_ID="${ELECTION_ID:-53815}"
OP_ENV=s6tljdcafkgqxfuxhcnj3m5x5a
export CLOUDFLARE_ACCOUNT_ID=e83f342a6c0f6e20b80697363714bf7b
export EV_SNAPSHOT_BUCKET=tx-turnout-ev-snapshots-preview

cd "$(git rev-parse --show-toplevel)"
uv sync --locked --extra waf --extra loader --quiet

run() { op run --environment "$OP_ENV" -- uv run python -m "texas_turnout_scraper.$1" "${@:2}"; }

report="$(mktemp)"
run early_vote_sync --election-id "$ELECTION_ID" --sync-mode backfill \
  --payload-kind statewide_roster --http-backend cloudscraper --apply --defer-kv-bump | tee "$report"
sync_sha="$(awk -F= '/^sha256=/{print $2; exit}' "$report")"
sync_status="$(awk -F= '/^status=/{print $2; exit}' "$report")"
[[ -n "$sync_sha" ]] || { echo "sync report has no sha256= line" >&2; exit 1; }

precompute=(--election-id "$ELECTION_ID" --apply --finalize-kv-digest "$sync_sha")
[[ "$sync_status" == "wrote" ]] && precompute+=(--finalize-kv-after-sync)
run early_vote_d1_precompute_backfill "${precompute[@]}"
run early_vote_roster_rows_backfill --election-id "$ELECTION_ID" --apply
run first_seen_build --election-id "$ELECTION_ID" --apply
run geo_load --election-id "$ELECTION_ID" --apply

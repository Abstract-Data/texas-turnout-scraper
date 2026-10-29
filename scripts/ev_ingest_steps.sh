#!/usr/bin/env bash
# The early-vote loader steps, in order (ADR-017). Run by the ingest Container and, under op run,
# by scripts/local_ev_backfill.sh. Prints counts and hashes only. Skips repeat_build, which is
# paused until after 2026-11-03 (ADR-014).
#   ev_ingest_steps.sh apply     # sync every day, precompute, roster rows, first-seen, geo
#   ev_ingest_steps.sh dry_run   # hash every day and print the plan; writes nothing
# Needs `python` with the package (waf + loader extras) on PATH and, for apply,
# CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, CLOUDFLARE_KV_NAMESPACE_ID, EV_SNAPSHOT_BUCKET,
# R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY in the environment.
set -euo pipefail

mode="${1:-}"
ELECTION_ID="${ELECTION_ID:-53815}"

step() { python -m "texas_turnout_scraper.$1" "${@:2}"; }
sync=(early_vote_sync --election-id "$ELECTION_ID" --sync-mode backfill
  --payload-kind statewide_roster --http-backend cloudscraper)

case "$mode" in
  dry_run)
    step "${sync[@]}" --dry-run
    exit 0
    ;;
  apply) ;;
  *)
    echo "usage: ev_ingest_steps.sh apply|dry_run" >&2
    exit 2
    ;;
esac

report="$(mktemp)"
trap 'rm -f "$report"' EXIT
step "${sync[@]}" --apply --defer-kv-bump | tee "$report"
sync_sha="$(sed -n 's/^sha256=//p' "$report" | head -n 1)"
sync_status="$(sed -n 's/^status=//p' "$report" | head -n 1)"
# Empty when no day was fetched and D1 holds none of them (see days_failed= in the report).
[[ -n "$sync_sha" ]] || { echo "sync published no day: nothing to precompute" >&2; exit 1; }

precompute=(--election-id "$ELECTION_ID" --apply --finalize-kv-digest "$sync_sha")
[[ "$sync_status" == "wrote" ]] && precompute+=(--finalize-kv-after-sync)
step early_vote_d1_precompute_backfill "${precompute[@]}"
step early_vote_roster_rows_backfill --election-id "$ELECTION_ID" --apply
step first_seen_build --election-id "$ELECTION_ID" --apply
step geo_load --election-id "$ELECTION_ID" --apply

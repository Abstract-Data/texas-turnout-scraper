#!/usr/bin/env bash
# Rebuild every flow chart in docs/flows/ with the pstack statechart skill, and stop on any change
# in which rules break. Needs Node, Java 11+ and tla2tools.jar.
#   STATECHART=/path/to/pstack/skills/statechart/scripts/statechart.mjs \
#   TLA2TOOLS=~/.local/share/tlaplus/tla2tools.jar JAVA=/opt/homebrew/opt/openjdk/bin/java \
#   bash docs/flows/regenerate.sh
set -euo pipefail
: "${STATECHART:?set STATECHART to statechart.mjs from the pstack statechart skill}"
: "${TLA2TOOLS:?set TLA2TOOLS to tla2tools.jar}"
cd "$(dirname "$0")"

node "$STATECHART" from-spec ev-ingest-run/EvIngestRun.tla \
  --title "Scheduled early-vote ingest run" \
  --out ev-ingest-run/chart --png
rm -rf ev-ingest-run/chart/tlc

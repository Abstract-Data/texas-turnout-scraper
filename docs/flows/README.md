# Flow charts

Each folder holds a TLA+ model of one flow, the labels that turn it into a chart, and the charts
the pstack `statechart` skill draws from it. TLC, the TLA+ model checker, explores every state of
the model and checks each rule. The model ties every condition to the code that checks it with a
file:line comment, so a reviewer can trace the chart back to the code.

Regenerate every chart (exits non-zero if a rule starts or stops breaking):

```bash
STATECHART=/path/to/pstack/skills/statechart/scripts/statechart.mjs \
TLA2TOOLS=~/.local/share/tlaplus/tla2tools.jar JAVA=/opt/homebrew/opt/openjdk/bin/java \
bash docs/flows/regenerate.sh
```

## Scheduled early-vote ingest run

[`ev-ingest-run/`](ev-ingest-run/): one cron-started run of the ingest Container
([ADR-017](../adr/017-cloudflare-container-ingest.md)), from the cron through the day-by-day sync
to the alarm that reads the exit code. The reader is the operator, who sees Workers Logs and the
read API.

- Model: [`EvIngestRun.tla`](ev-ingest-run/EvIngestRun.tla) (two EV days, two runs),
  [`EvIngestRun.cfg`](ev-ingest-run/EvIngestRun.cfg), labels in
  [`EvIngestRun.map.mjs`](ev-ingest-run/EvIngestRun.map.mjs)
- Charts: [overview](ev-ingest-run/chart/chart.png) ([source](ev-ingest-run/chart/chart.mmd)),
  [bug 1](ev-ingest-run/chart/bug-1.png) ([source](ev-ingest-run/chart/bug-1.mmd)),
  [`stories.md`](ev-ingest-run/chart/stories.md), [`summary.json`](ev-ingest-run/chart/summary.json)
- Size: 52 model states and 58 transitions, drawn as 12 chart states in two parallel regions;
  11 work and 1 breaks a rule.

### Rules

| Rule | Sentence | Result |
|---|---|---|
| `NoStrandedDay` | Every day the portal handed over during a run is in the API once that run ends. | **Broken** (bug 1) |
| `FailureIsLogged` | A run that fails says so in the logs. | Holds |

### Bug 1: one portal 503 strands the days already downloaded

Rule: `NoStrandedDay`. Steps, as [bug-1](ev-ingest-run/chart/bug-1.png) numbers them:

1. The cron fires.
2. A day downloads (written to R2 and the D1 day tables).
3. The portal keeps returning 503 for the next day past all 5 attempts (`early_vote_sync.py:126-127`).
   The sync raises, and `set -e` with `pipefail` ends `ev_ingest_steps.sh` at line 33 with exit 1.
4. The alarm logs `ev-ingest finished` with `exit_code: 1`. The precompute never runs, so the KV
   version is never bumped (`--defer-kv-bump`, `ev_ingest_steps.sh:33`, `:38-40`), and the read API
   keeps serving the body cached for the old version.

**Reproduced in production.** The run started 2026-10-28 22:00 UTC. Its Workers Logs show steps 1–4
in order: 10/16 and 10/17 `status=wrote`, a 503 on `getFile` for 10/19, then `exit_code: 1` after
120 s. The run at 15:00 UTC that day failed the same way on 10/24. Reading the code, not a run,
proves that the KV version stayed put: the precompute step is the only one that bumps it, and it
never started. The days reached a new version at the next successful run (2026-10-29 03:00 UTC).

**Caveat.** The sync writes each day's D1 rows as it goes. A request that misses the KV cache (TTL
24 h) builds its body from D1 and can already include a stranded day, and caches that under the old
version. So "not published" means "not under a new version", not "absent from D1".

No fix is open. Candidate fixes: let a run skip a day the portal keeps refusing and publish the
rest, or retry a failed day for longer.

### What the model leaves out

- Days the portal returns empty (`skipped_empty`), the dry-run mode, `/spike`, `POST /run` (it takes
  the same `startRun` path as the cron) and `GET /status`.
- The 3-hour timeout, `ev-ingest lost` and `ev-ingest check failed`, a deploy during a run, and
  the inactivity timeout.
- The steps after the precompute (roster rows, first-seen, geo), which the model folds into one
  publish step.
- The schedule gate is a fixed guard: the model does not check the cron hours or the 2026-11-03
  cutoff (`schedule.ts` has its own unit tests).

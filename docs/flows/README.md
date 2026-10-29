# Flow charts

Each folder holds a TLA+ model of one flow, the labels that turn it into a chart, and the charts
the pstack `statechart` skill draws from it. TLC, the TLA+ model checker, explores every state of
the model and checks each rule. The model ties every condition to the code that checks it with a
file:line comment, so a reviewer can trace the chart back to the code.

Regenerate every chart (exits non-zero if a rule breaks):

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
  [`stories.md`](ev-ingest-run/chart/stories.md), [`summary.json`](ev-ingest-run/chart/summary.json)
- Size: 87 model states, drawn as 13 chart states in two parallel regions; none breaks a rule.

### Rules

| Rule | Sentence | Result |
|---|---|---|
| `NoStrandedDay` | Every day the portal handed over during a run is in the API once that run ends. | Holds (broken before the fix for bug 1) |
| `FailureIsLogged` | A run that skips a day says so in the logs. | Holds |

### Bug 1 (fixed): one portal 503 stranded the days already downloaded

Rule: `NoStrandedDay`. Before the fix, TLC found this path:

1. The cron fires.
2. A day downloads (written to R2 and the D1 day tables).
3. The portal keeps returning 503 for the next day past all 5 attempts (`early_vote_sync.py:126-127`).
   The sync raised, and `set -e` with `pipefail` ended `ev_ingest_steps.sh` at line 33 with exit 1.
4. The alarm logged `ev-ingest finished` with `exit_code: 1`. The precompute never ran, so the KV
   version was never bumped, and the read API kept serving the body cached for the old version.

**Reproduced in production.** The run that started 2026-10-28 22:00 UTC shows steps 1–4 in Workers
Logs: 10/16 and 10/17 `status=wrote`, a 503 on `getFile` for 10/19, then `exit_code: 1` after 120 s.
The 15:00 UTC run that day failed the same way on 10/24.

**The fix.** `run_backfill` now catches the fetch error for that day
(`early_vote_backfill.py:283-302`), lists it in `failed_dates=` and moves on. The day keeps what D1
last stored for it, and if D1 has a stored hash for it, that hash stays in the manifest and in the
published `sha256=`, so a failed newest day does not move the published hash back to an older day.
The run exits 0 and the alarm logs `days_failed=` and `failed_dates=` with the report. Errors that
are not the portal's (a D1 write, for example) still end the run with exit 1. If every day fails and
D1 holds none of them, there is nothing to publish and the script exits 1
(`ev_ingest_steps.sh:37`).

**Caveat.** The sync writes each day's D1 rows as it goes. A request that misses the KV cache (TTL
24 h) builds its body from D1 and can include a day before the precompute publishes it, and caches
that under the old version. So "published" means "under a new KV version", not "present in D1".

### What the model leaves out

- Days the portal returns empty (`skipped_empty`), the dry-run mode, `/spike`, `POST /run` (it takes
  the same `startRun` path as the cron) and `GET /status`.
- Errors that are not the portal's, such as a D1 write failing mid-sync. They still end the run
  with exit 1 and strand the days already downloaded; the model has no action for them.
- Whether D1 already holds a failed day: the model starts every day at "old", so the
  every-day-fails-with-nothing-stored exit is not drawn.
- The 3-hour timeout, `ev-ingest lost` and `ev-ingest check failed`, a deploy during a run, and
  the inactivity timeout.
- The steps after the precompute (roster rows, first-seen, geo), which the model folds into one
  publish step.
- The schedule gate is a fixed guard: the model does not check the cron hours or the 2026-11-03
  cutoff (`schedule.ts` has its own unit tests).

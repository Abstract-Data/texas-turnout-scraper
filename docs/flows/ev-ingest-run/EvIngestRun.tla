---------------------------- MODULE EvIngestRun ----------------------------
\* Code model of one scheduled early-vote ingest run (ADR-017).
\* Worker + Durable Object: infra/ev_ingest/src/index.ts
\* Container steps: scripts/ev_ingest_steps.sh -> early_vote_backfill.run_backfill
\* Days stands for the EV days in the load plan, fetched in ascending order.
EXTENDS Naturals

CONSTANTS N, MaxRuns
Days == 1..N

VARIABLES
    container,  \* "stopped" | "running"      index.ts:114 container.running
    exit,       \* "none" | "ok"              schedule.ts:43 /tmp/ev_ingest.exit
    cursor,     \* next day the sync fetches  early_vote_backfill.py:275
    portal,     \* per day: "up" | "down" (503 past every retry) for this run
    r2,         \* per day: "old" | "new" snapshot in R2 / D1 day chunks
    api,        \* per day: "old" | "new" in the body the read API serves for the published
                \* KV version (read.py:179-192 in infra/preview/worker/src/ev_api)
    served,     \* per day: the portal returned it during the current run
    skipped,    \* the current run skipped a day the portal refused (failed_dates=)
    logged,     \* "none" | "started" | "finished_ok" | "finished_skipped" (days_failed>0)
    runs

vars == <<container, exit, cursor, portal, r2, api, served, skipped, logged, runs>>

Init ==
    /\ container = "stopped"
    /\ exit = "none"
    /\ cursor = 1
    /\ portal = [d \in Days |-> "up"]
    /\ r2 = [d \in Days |-> "old"]
    /\ api = [d \in Days |-> "old"]
    /\ served = [d \in Days |-> FALSE]
    /\ skipped = FALSE
    /\ logged = "none"
    /\ runs = 0

\* The cron fires in a run hour (schedule.ts:21-23, index.ts:241) with EV_SYNC_MODE=apply
\* (index.ts:245); startRun starts the Container (index.ts:117) and logs "ev-ingest started"
\* (index.ts:144). The portal's health for the run is chosen here.
StartRun ==
    /\ container = "stopped"                    \* index.ts:114
    /\ runs < MaxRuns
    /\ \E p \in [Days -> {"up", "down"}] : portal' = p
    /\ container' = "running"
    /\ exit' = "none"
    /\ cursor' = 1
    /\ served' = [d \in Days |-> FALSE]
    /\ skipped' = FALSE
    /\ logged' = "started"
    /\ runs' = runs + 1
    /\ UNCHANGED <<r2, api>>

\* One day downloads and is written to R2 and the D1 day tables (early_vote_backfill.py:277-326).
FetchDay ==
    /\ container = "running"
    /\ exit = "none"                            \* ev_ingest_steps.sh:33 still in the sync step
    /\ cursor <= N                              \* early_vote_backfill.py:275
    /\ portal[cursor] = "up"
    /\ r2' = [r2 EXCEPT ![cursor] = "new"]
    /\ served' = [served EXCEPT ![cursor] = TRUE]
    /\ cursor' = cursor + 1
    /\ UNCHANGED <<container, exit, portal, api, skipped, logged, runs>>

\* The portal keeps returning 503 past every retry (5 attempts, early_vote_sync.py:126-127,
\* http_transport.py:227-230). The sync catches it, records the day in failed_dates= and moves on;
\* the day keeps what D1 last stored for it (early_vote_backfill.py:283-302).
DayFails ==
    /\ container = "running"
    /\ exit = "none"                            \* ev_ingest_steps.sh:33
    /\ cursor <= N                              \* early_vote_backfill.py:275
    /\ portal[cursor] = "down"                  \* http_transport.py:227
    /\ cursor' = cursor + 1
    /\ skipped' = TRUE
    /\ UNCHANGED <<container, exit, portal, r2, api, served, logged, runs>>

\* Every day tried: the precompute finalizes and bumps the KV version the read API caches under
\* (ev_ingest_steps.sh:39-41), then roster rows, first-seen and geo (:42-44); the script exits 0.
\* The sync runs with --defer-kv-bump (:33), so no day reaches a new version before this.
Publish ==
    /\ container = "running"
    /\ exit = "none"                            \* ev_ingest_steps.sh:37
    /\ cursor = N + 1                           \* early_vote_backfill.py:275 loop done
    /\ api' = [d \in Days |-> IF r2[d] = "new" THEN "new" ELSE api[d]]
    /\ exit' = "ok"
    /\ UNCHANGED <<container, cursor, portal, r2, served, skipped, logged, runs>>

\* The 60 s alarm reads the exit file, logs "ev-ingest finished" with the report lines, including
\* days_failed= and failed_dates= (schedule.ts:53-58), and destroys the Container (index.ts:168-177).
AlarmFinish ==
    /\ container = "running"
    /\ exit # "none"                            \* index.ts:169
    /\ container' = "stopped"
    /\ logged' = IF skipped THEN "finished_skipped" ELSE "finished_ok"
    /\ UNCHANGED <<exit, cursor, portal, r2, api, served, skipped, runs>>

Next == StartRun \/ FetchDay \/ DayFails \/ Publish \/ AlarmFinish
Spec == Init /\ [][Next]_vars

\* Every day the portal handed over during a run is in the API once that run ends.
NoStrandedDay ==
    container = "stopped" => \A d \in Days : served[d] => api[d] = "new"

\* A run that skips a day says so in the logs.
FailureIsLogged ==
    (container = "stopped" /\ skipped) => logged = "finished_skipped"
=============================================================================

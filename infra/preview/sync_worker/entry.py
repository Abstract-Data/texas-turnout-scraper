"""Cloudflare Python Worker: early-vote sync cron + EvSyncWorkflow (ADR-014).

Separate script ``tx-turnout-ev-sync-preview`` — no HTTP routes. The read API Worker is unchanged
until Terraform repoints the cron trigger (infra PR).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ev_sync.cron import run_scheduled
from ev_sync.workflow_entry import install_workflow


def install_entrypoint() -> type | None:
    try:
        from workers import WorkerEntrypoint
    except ImportError:
        return None

    class Default(WorkerEntrypoint):
        async def scheduled(self, controller, env, ctx):
            now = datetime.fromtimestamp(controller.scheduledTime / 1000, timezone.utc)
            await run_scheduled(env, now)

    return Default


Default = install_entrypoint()
EvSyncWorkflow = install_workflow()

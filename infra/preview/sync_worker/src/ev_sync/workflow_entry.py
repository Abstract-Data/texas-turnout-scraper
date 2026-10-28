from __future__ import annotations

from typing import Any

from texas_turnout_scraper.early_vote_workflow.mode import EvSyncMode, parse_ev_sync_mode
from texas_turnout_scraper.early_vote_workflow.plan import (
    ELECTION_53815,
    build_instance_id,
    make_run_plan,
)
from texas_turnout_scraper.early_vote_workflow.runner import run_workflow_sync

WORKFLOW_BINDING = "EV_SYNC_WORKFLOW"
STEP_CONFIG: dict[str, Any] = {
    "retries": {"limit": 3, "delay": "30 seconds", "backoff": "constant"},
    "timeout": "300 seconds",
}


class _SyntheticSource:
    """Workflow placeholder until portal bindings are wired in apply mode."""

    def fetch_day_bytes(self, ev_date: str) -> bytes:
        raise NotImplementedError("portal fetch is not implemented in this build")

    def stored_day_sha256(self, ev_date: str) -> str | None:
        return None


async def run_ev_sync_workflow(event: dict[str, object], step: Any) -> dict[str, object]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    mode: EvSyncMode = parse_ev_sync_mode(payload.get("mode") or payload.get("EV_SYNC_MODE"))
    election_id = str(payload.get("election_id") or ELECTION_53815)
    run_mode = str(payload.get("run_mode") or "sweep")
    run_id = str(payload.get("run_id") or build_instance_id(election_id, _utc_now()))
    calendar_dates: tuple = ()  # filled from D1 index in a later step

    @step.do("ev-sync-plan", config=STEP_CONFIG)
    async def plan_step():
        if mode == "off":
            return {"status": "off", "run_id": run_id}
        return {
            "run_id": run_id,
            "election_id": election_id,
            "mode": mode,
            "run_mode": run_mode,
        }

    plan_out = await plan_step()
    if plan_out.get("status") == "off":
        return plan_out

    @step.do("ev-sync-run", config=STEP_CONFIG)
    async def run_step():
        plan = make_run_plan(
            election_id=election_id,
            run_id=run_id,
            run_mode="sweep" if run_mode == "sweep" else "recent",
            calendar_dates=calendar_dates,
            today=_utc_now().date(),
        )
        report = run_workflow_sync(plan, mode, _SyntheticSource())
        return {
            "run_id": report.run_id,
            "mode": report.mode,
            "days": len(report.days),
            "kv_bump": report.kv_bump,
            "skipped_locked": report.skipped_locked,
        }

    return await run_step()


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc)


def install_workflow() -> type | None:
    try:
        from workers import WorkflowEntrypoint
    except ImportError:
        return None

    class EvSyncWorkflow(WorkflowEntrypoint):
        async def run(self, event, step):
            return await run_ev_sync_workflow(event, step)

    return EvSyncWorkflow

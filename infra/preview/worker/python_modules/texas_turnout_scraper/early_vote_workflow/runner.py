from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from texas_turnout_scraper.early_vote_workflow.dry_run import (
    dry_run_day_from_bytes,
)
from texas_turnout_scraper.early_vote_workflow.hash_gate import day_unchanged
from texas_turnout_scraper.early_vote_workflow.kv_finalize import workflow_should_bump_kv
from texas_turnout_scraper.early_vote_workflow.markers import StepMarker
from texas_turnout_scraper.early_vote_workflow.mode import (
    EvSyncMode,
    portal_fetch_enabled,
    writes_enabled,
)
from texas_turnout_scraper.early_vote_workflow.plan import RunPlan
from texas_turnout_scraper.early_vote_workflow.run_state import RunState


class DaySource(Protocol):
    def fetch_day_bytes(self, ev_date: str) -> bytes: ...

    def stored_day_sha256(self, ev_date: str) -> str | None: ...


@dataclass(frozen=True)
class DayOutcome:
    ev_date: str
    status: str
    sha256: str
    decoded_bytes: int
    est_rows: int


@dataclass(frozen=True)
class RunReport:
    election_id: str
    run_id: str
    mode: str
    days: tuple[DayOutcome, ...]
    kv_bump: bool
    skipped_locked: bool


def run_plan_off(_plan: RunPlan) -> RunReport:
    return RunReport(
        election_id=_plan.election_id,
        run_id=_plan.run_id,
        mode="off",
        days=(),
        kv_bump=False,
        skipped_locked=False,
    )


def run_workflow_sync(
    plan: RunPlan,
    mode: EvSyncMode,
    source: DaySource,
    *,
    state: RunState | None = None,
    kv_stored_hash: str | None = None,
    pending_before: bool = False,
    pending_after: bool = False,
    finalize_after_sync: bool = False,
    lease_acquired: bool = True,
) -> RunReport:
    """Synchronous orchestration used by unit tests (no Civix / bindings)."""
    if mode == "off":
        return run_plan_off(plan)
    if not lease_acquired:
        return RunReport(
            election_id=plan.election_id,
            run_id=plan.run_id,
            mode=mode,
            days=(),
            kv_bump=False,
            skipped_locked=True,
        )
    run_state = state or RunState()
    outcomes: list[DayOutcome] = []
    published_digest = ""
    for ev_date in plan.days:
        stage_marker = StepMarker(plan.run_id, ev_date, "stage")
        if run_state.done(stage_marker):
            outcomes.append(
                DayOutcome(ev_date, "resumed", "", 0, 0),
            )
            continue
        if not portal_fetch_enabled(mode):
            continue
        raw = source.fetch_day_bytes(ev_date)
        stored = source.stored_day_sha256(ev_date)
        report = dry_run_day_from_bytes(ev_date, raw, stored_sha256=stored)
        if report.status == "skipped_empty":
            run_state.days_failed += 0
            outcomes.append(
                DayOutcome(
                    ev_date,
                    report.status,
                    report.sha256,
                    report.decoded_bytes,
                    report.est_rows,
                )
            )
            run_state.mark_done(stage_marker)
            continue
        if report.unchanged:
            run_state.days_unchanged += 1
            outcomes.append(
                DayOutcome(
                    ev_date,
                    "unchanged",
                    report.sha256,
                    report.decoded_bytes,
                    report.est_rows,
                )
            )
            run_state.mark_done(stage_marker)
            continue
        outcomes.append(
            DayOutcome(ev_date, "changed", report.sha256, report.decoded_bytes, report.est_rows)
        )
        run_state.days_changed += 1
        if writes_enabled(mode):
            run_state.files_written += 1
            published_digest = report.sha256
        run_state.mark_done(stage_marker)
        commit_marker = StepMarker(plan.run_id, ev_date, "commit")
        run_state.mark_done(commit_marker)
    kv_bump = False
    if writes_enabled(mode):
        kv_bump = workflow_should_bump_kv(
            published_digest=published_digest,
            files_written=run_state.files_written,
            finalize_after_sync=finalize_after_sync,
            pending_before=pending_before,
            pending_after=pending_after,
            kv_stored_hash=kv_stored_hash,
        )
    return RunReport(
        election_id=plan.election_id,
        run_id=plan.run_id,
        mode=mode,
        days=tuple(outcomes),
        kv_bump=kv_bump,
        skipped_locked=False,
    )


def hash_short_circuit(stored: str | None, canonical_sha: str) -> bool:
    return day_unchanged(stored, canonical_sha)

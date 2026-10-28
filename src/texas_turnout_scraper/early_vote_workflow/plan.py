from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from texas_turnout_scraper.early_vote_backfill import plan_backfill_load_dates
from texas_turnout_scraper.early_vote_sync import iso_ev_date

RunMode = Literal["recent", "sweep"]
ELECTION_53815 = "53815"


@dataclass(frozen=True)
class RunPlan:
    election_id: str
    run_id: str
    mode: RunMode
    days: tuple[str, ...]


def build_instance_id(election_id: str, when: datetime) -> str:
    """Deterministic Workflow id ``ev-{election}-{yyyymmddhh}`` (UTC hour)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%d%H")
    return f"ev-{election_id}-{stamp}"


def build_backfill_instance_id(election_id: str, when: datetime) -> str:
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%d%H")
    return f"ev-{election_id}-backfill-{stamp}"


def resolve_run_mode(central_hour: int) -> RunMode:
    return "sweep" if central_hour == 22 else "recent"


def _recent_days(all_days: tuple[str, ...], today: date) -> tuple[str, ...]:
    yesterday = today - timedelta(days=1)
    targets = {iso_ev_date(today), iso_ev_date(yesterday)}
    return tuple(day for day in all_days if day in targets)


def plan_run_days(
    *,
    election_id: str,
    calendar_dates: tuple[date, ...],
    run_mode: RunMode,
    today: date,
) -> tuple[str, ...]:
    """Ordered EV dates to process (ISO), skipping future days."""
    plan = plan_backfill_load_dates(election_id, calendar_dates, today=today)
    all_days = plan.load_dates_iso
    if run_mode == "sweep":
        return all_days
    return _recent_days(all_days, today)


def make_run_plan(
    *,
    election_id: str,
    run_id: str,
    run_mode: RunMode,
    calendar_dates: tuple[date, ...],
    today: date,
) -> RunPlan:
    return RunPlan(
        election_id=election_id,
        run_id=run_id,
        mode=run_mode,
        days=plan_run_days(
            election_id=election_id,
            calendar_dates=calendar_dates,
            run_mode=run_mode,
            today=today,
        ),
    )

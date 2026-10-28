from __future__ import annotations

import logging
from datetime import datetime

from texas_turnout_scraper.early_vote_workflow.cron_schedule import should_dispatch
from texas_turnout_scraper.early_vote_workflow.mode import parse_ev_sync_mode
from texas_turnout_scraper.early_vote_workflow.plan import (
    ELECTION_53815,
    build_instance_id,
    resolve_run_mode,
)

logger = logging.getLogger(__name__)


async def run_scheduled(env: object, now: datetime) -> None:
    if not should_dispatch(now):
        return
    mode = parse_ev_sync_mode(getattr(env, "EV_SYNC_MODE", "off"))
    if mode == "off":
        logger.info("ev-sync cron skipped: EV_SYNC_MODE=off")
        return
    binding = getattr(env, "EV_SYNC", None)
    if binding is None:
        logger.warning("ev-sync cron skipped: EV_SYNC binding missing")
        return
    from texas_turnout_scraper.early_vote_workflow.cron_schedule import central_hour

    run_mode = resolve_run_mode(central_hour(now))
    instance_id = build_instance_id(ELECTION_53815, now)
    await binding.create(
        id=instance_id,
        params={"mode": mode, "run_mode": run_mode, "election_id": ELECTION_53815},
    )
    logger.info("ev-sync workflow created run_id=%s mode=%s", instance_id, mode)

"""Cron trigger: dispatch the early-vote sync workflow at 10:00, 17:00 and 22:00 Central.

The Worker only starts the GitHub Actions run; scraping and the D1/KV writes stay in
Actions. Cron is UTC only, so the trigger fires on both the CDT and the CST hours and
``should_dispatch`` keeps the three that are Central 10, 17 and 22 on that date. The
offset comes from the one DST change in the window, not from tzdata.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CRON_SCHEDULE = "0 15,16,22,23,3,4 * * *"

_CST_FROM = datetime(2026, 11, 1, 7, tzinfo=timezone.utc)
_LAST_RUN = datetime(2026, 11, 4, 5, 59, 59, tzinfo=timezone.utc)
_CENTRAL_HOURS = frozenset({10, 17, 22})

_DISPATCH_URL = (
    "https://api.github.com/repos/Abstract-Data/texas-turnout-scraper"
    "/actions/workflows/early-vote-sync.yml/dispatches"
)
_INPUTS = {
    "sync_mode": "backfill",
    "election_id": "53815",
    "election_date": "",
    "dry_run": "false",
    "payload_kind": "statewide_roster",
    "trigger": "cron",
}

Send = Callable[[str, dict[str, str], dict[str, object]], Awaitable[int]]


class DispatchError(RuntimeError):
    """GitHub did not accept the workflow dispatch."""


def should_dispatch(now: datetime) -> bool:
    utc_offset = 5 if now < _CST_FROM else 6
    return now <= _LAST_RUN and (now.hour - utc_offset) % 24 in _CENTRAL_HOURS


def dispatch_request(token: str) -> tuple[str, dict[str, str], dict[str, object]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        # Pinned: this version answers a dispatch with 204; 2026-03-10 answers 200.
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tx-turnout-ev-api-preview-cron",
    }
    return _DISPATCH_URL, headers, {"ref": "main", "inputs": dict(_INPUTS)}


async def run_scheduled(env: object, now: datetime, send: Send) -> None:
    if not should_dispatch(now):
        return
    token = getattr(env, "GITHUB_DISPATCH_TOKEN", None)
    if not token:
        logger.warning("early-vote dispatch skipped: GITHUB_DISPATCH_TOKEN is not set")
        return
    status = await send(*dispatch_request(token))
    if status != 204:
        raise DispatchError(f"early-vote dispatch failed: HTTP {status}")
    logger.info("early-vote dispatch accepted: HTTP 204")

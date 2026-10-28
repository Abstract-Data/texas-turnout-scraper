"""DST-safe Central schedule gate (ADR-013). Shared by sync and read Workers."""

from __future__ import annotations

from datetime import datetime, timezone

CRON_SCHEDULE = "0 15,16,22,23,3,4 * * *"

_CST_FROM = datetime(2026, 11, 1, 7, tzinfo=timezone.utc)
_LAST_RUN = datetime(2026, 11, 4, 5, 59, 59, tzinfo=timezone.utc)
_CENTRAL_HOURS = frozenset({10, 17, 22})


def should_dispatch(now: datetime) -> bool:
    utc_offset = 5 if now < _CST_FROM else 6
    return now <= _LAST_RUN and (now.hour - utc_offset) % 24 in _CENTRAL_HOURS


def central_hour(now: datetime) -> int:
    utc_offset = 5 if now < _CST_FROM else 6
    return (now.hour - utc_offset) % 24

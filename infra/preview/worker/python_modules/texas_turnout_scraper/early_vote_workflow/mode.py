from __future__ import annotations

from typing import Literal

EvSyncMode = Literal["off", "dry_run", "apply"]


def parse_ev_sync_mode(raw: object) -> EvSyncMode:
    """Parse ``EV_SYNC_MODE``. Unknown values are treated as ``off``."""
    if not isinstance(raw, str):
        return "off"
    value = raw.strip().lower()
    if value in ("off", "dry_run", "apply"):
        return value
    return "off"


def writes_enabled(mode: EvSyncMode) -> bool:
    """Whether D1/KV/R2 mutations are allowed (``apply`` only)."""
    return mode == "apply"


def portal_fetch_enabled(mode: EvSyncMode) -> bool:
    """Whether the run may download from Civix (``dry_run`` and ``apply``)."""
    return mode in ("dry_run", "apply")

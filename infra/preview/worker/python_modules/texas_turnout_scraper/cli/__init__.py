"""Texas turnout CLI package."""

from __future__ import annotations

from . import voterfile as _voterfile  # noqa: F401 — registers voterfile subcommands
from ._common import (
    _build_civix_index_entries,
    _exit_on_partial_fetch_failures,
    _legacy_election_date,
    _load_index_sections,
    _parse_ev_date,
    _prompt_select,
    _roster_is_fresh,
    _sort_civix_elections,
    _sort_civix_ev_dates,
    _update_election_index,
    _write_election_index,
)
from ._entry import main
from ._typer_apps import app
from .audit import audit_run, audit_run_inline
from .civix import civix_fetch_all, civix_turnout_fetch
from .legacy import (
    _exit_on_legacy_api_error,
    legacy_fetch_all,
    legacy_refresh_all,
    legacy_roster_fetch,
    legacy_turnout_fetch,
)

__all__ = [
    "_build_civix_index_entries",
    "_exit_on_legacy_api_error",
    "_exit_on_partial_fetch_failures",
    "_legacy_election_date",
    "_load_index_sections",
    "_parse_ev_date",
    "_prompt_select",
    "_roster_is_fresh",
    "_sort_civix_elections",
    "_sort_civix_ev_dates",
    "_update_election_index",
    "_write_election_index",
    "app",
    "audit_run",
    "audit_run_inline",
    "civix_fetch_all",
    "civix_turnout_fetch",
    "legacy_fetch_all",
    "legacy_refresh_all",
    "legacy_roster_fetch",
    "legacy_turnout_fetch",
    "main",
]

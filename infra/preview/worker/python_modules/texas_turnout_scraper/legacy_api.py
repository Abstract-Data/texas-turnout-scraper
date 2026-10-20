"""High-level legacy SOS portal API for CLI, MCP, and scheduled workflows.

Wraps :class:`~texas_turnout_scraper.session.LegacySession` session management
and Struts step-2 priming so callers do not need to manage cookies or POST order.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from .elections import list_elections as _list_elections
from .http_transport import HttpBackend
from .models import CountyRoster, CountyTurnout, LegacyElection
from .roster import fetch_roster_strategy_a, fetch_roster_strategy_b
from .session import LegacySession
from .turnout import extract_county_ids, fetch_ev_details_html, fetch_turnout
from .writer import write_roster_csv

Strategy = Literal["A", "B"]


def list_elections(
    *,
    http_backend: HttpBackend = "cloudscraper",
    pace_seconds: float = LegacySession.DEFAULT_PACE,
) -> list[LegacyElection]:
    """List elections from the legacy SOS portal dropdown."""
    with LegacySession(pace_seconds=pace_seconds, http_backend=http_backend) as session:
        return _list_elections(session)


def fetch_county_turnout(
    source_election_id: str,
    ev_date: date,
    *,
    http_backend: HttpBackend = "cloudscraper",
    pace_seconds: float = LegacySession.DEFAULT_PACE,
) -> list[CountyTurnout]:
    """Fetch county turnout for one election date (session + priming included)."""
    with LegacySession(pace_seconds=pace_seconds, http_backend=http_backend) as session:
        return fetch_turnout(session, source_election_id, ev_date)


def fetch_roster(
    source_election_id: str,
    ev_date: date,
    *,
    strategy: Strategy = "A",
    county_ids: list[str] | None = None,
    out_dir: Path | None = None,
    http_backend: HttpBackend = "cloudscraper",
    pace_seconds: float = LegacySession.DEFAULT_PACE,
) -> list[CountyRoster]:
    """Fetch voter rosters via Strategy A (per-county) or Strategy B (bulk ZIP).

    Strategy A resolves ``county_ids`` from the turnout page when omitted, using
    ``townId`` values from :func:`~texas_turnout_scraper.turnout.extract_county_ids`.

    Strategy B streams a bulk ZIP to ``out_dir`` and returns an empty list
    (roster rows are inside the ZIP file, not parsed here).
    """
    strategy_upper = strategy.upper()
    if strategy_upper not in ("A", "B"):
        msg = f"strategy must be 'A' or 'B', got {strategy!r}"
        raise ValueError(msg)

    with LegacySession(pace_seconds=pace_seconds, http_backend=http_backend) as session:
        if strategy_upper == "B":
            if out_dir is None:
                msg = "out_dir is required for Strategy B bulk ZIP download"
                raise ValueError(msg)
            session.prime_election(source_election_id)
            fetch_roster_strategy_b(session, source_election_id, ev_date, out_dir)
            return []

        resolved_ids = county_ids
        county_names: dict[str, str] | None = None
        skip_prime = False
        if resolved_ids is None:
            html = fetch_ev_details_html(session, source_election_id, ev_date)
            id_by_name = extract_county_ids(html)
            if not id_by_name:
                msg = (
                    f"No county IDs found in turnout HTML for election "
                    f"{source_election_id} on {ev_date}"
                )
                raise ValueError(msg)
            county_names = {county_id: name for name, county_id in id_by_name.items()}
            resolved_ids = list(id_by_name.values())
            skip_prime = True

        rosters = fetch_roster_strategy_a(
            session,
            source_election_id,
            ev_date,
            resolved_ids,
            pace_seconds=pace_seconds,
            county_names=county_names,
            skip_prime=skip_prime,
        )

        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            date_str = ev_date.isoformat()
            for roster in rosters:
                out_path = out_dir / f"roster_{date_str}_{roster.county}.csv"
                write_roster_csv(roster.records, out_path)

        return rosters


def fetch_single_county_roster(
    source_election_id: str,
    ev_date: date,
    county_id: str,
    county_name: str | None = None,
    *,
    http_backend: HttpBackend = "cloudscraper",
    pace_seconds: float = LegacySession.DEFAULT_PACE,
) -> CountyRoster:
    """Fetch one county roster via Strategy A (session + priming included)."""
    names = {county_id: county_name} if county_name else None
    with LegacySession(pace_seconds=pace_seconds, http_backend=http_backend) as session:
        rosters = fetch_roster_strategy_a(
            session,
            source_election_id,
            ev_date,
            [county_id],
            pace_seconds=pace_seconds,
            county_names=names,
        )

    if not rosters:
        msg = (
            f"No roster returned for county_id={county_id} "
            f"on election {source_election_id} date {ev_date}"
        )
        raise ValueError(msg)
    return rosters[0]

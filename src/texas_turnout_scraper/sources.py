"""Roster source protocol — shared fetch-all surface for Civix and legacy."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from . import legacy_api
from .civix import CivixClient, CivixElection, fetch_county_roster
from .enums import Source
from .http_transport import HTTP_FETCH_EXCEPTIONS, format_fetch_error
from .models import CountyRoster
from .roster import fetch_roster_strategy_a
from .session import LegacySession
from .turnout import extract_county_ids, fetch_ev_details_html

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CountyFetchFailure:
    """One county/date fetch failure (no PII in label)."""

    label: str


class RosterSource(Protocol):
    """Protocol for per-source roster fetch-all pipelines."""

    source_prefix: str

    def list_elections(self) -> Sequence[object]: ...

    def fetch_election_rosters(
        self,
        election_id: str,
        *,
        pace_seconds: float = 1.0,
        county_ids: list[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[list[CountyRoster], list[CountyFetchFailure]]: ...


class CivixSource:
    """Civix EVR implementation of :class:`RosterSource`."""

    source_prefix = Source.CIVIX.value

    def __init__(
        self,
        *,
        pace_seconds: float = 1.0,
        http_backend: str = "cloudscraper",
    ) -> None:
        self._pace_seconds = pace_seconds
        self._http_backend = http_backend

    def list_elections(self) -> list[CivixElection]:
        with CivixClient(
            pace_seconds=self._pace_seconds,
            http_backend=self._http_backend,  # type: ignore[arg-type]
        ) as client:
            return client.list_elections()

    def fetch_election_rosters(
        self,
        election_id: str,
        *,
        pace_seconds: float | None = None,
        county_ids: list[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[list[CountyRoster], list[CountyFetchFailure]]:
        del county_ids  # legacy-only filter; Civix uses turnout roster_available
        pace = pace_seconds if pace_seconds is not None else self._pace_seconds
        failures: list[CountyFetchFailure] = []
        all_rosters: list[CountyRoster] = []

        with CivixClient(
            pace_seconds=pace,
            http_backend=self._http_backend,  # type: ignore[arg-type]
        ) as client:
            elections = client.list_elections()
            election = next(
                (
                    e
                    for e in elections
                    if str(e.id) == election_id or e.source_election_id == election_id
                ),
                None,
            )
            if election is None:
                msg = f"Election {election_id} not found"
                raise ValueError(msg)

            for ev in election.early_voting_dates:
                ev_date = ev.date
                try:
                    turnout_rows = client.fetch_ev_turnout(
                        election_id=election.id,
                        election_date=ev_date,
                    )
                except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                    detail = format_fetch_error(exc)
                    failures.append(
                        CountyFetchFailure(label=f"{ev_date}: turnout fetch failed ({detail})"),
                    )
                    logger.warning(
                        "EV turnout fetch failed for election %s on %s: %s",
                        election_id,
                        ev_date,
                        detail,
                    )
                    continue

                roster_counties = [r for r in turnout_rows if r.roster_available]
                if on_progress:
                    on_progress(
                        f"[{ev_date}] Fetching {len(roster_counties)} counties...",
                    )

                date_rosters: list[CountyRoster] = []
                for county_row in roster_counties:
                    try:
                        roster = fetch_county_roster(
                            client,
                            election_id=election.id,
                            election_date=ev_date,
                            county_name=county_row.county,
                            county_id=county_row.county_id,
                        )
                        date_rosters.append(roster)
                    except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                        detail = format_fetch_error(exc)
                        label = f"{county_row.county}/{ev_date}: {detail}"
                        failures.append(CountyFetchFailure(label=label))
                        logger.warning(
                            "County roster fetch failed for %s on %s: %s",
                            county_row.county,
                            ev_date,
                            detail,
                        )

                if not date_rosters:
                    failures.append(
                        CountyFetchFailure(label=f"{ev_date}: no county rosters fetched"),
                    )
                elif on_progress:
                    records = sum(len(r.records) for r in date_rosters)
                    on_progress(f"  done ({records:,} records)")

                all_rosters.extend(date_rosters)

        return all_rosters, failures


class LegacySource:
    """Legacy SOS HTML portal implementation of :class:`RosterSource`."""

    source_prefix = Source.LEGACY.value

    def __init__(
        self,
        *,
        pace_seconds: float = LegacySession.DEFAULT_PACE,
        http_backend: str = "cloudscraper",
    ) -> None:
        self._pace_seconds = pace_seconds
        self._http_backend = http_backend

    def list_elections(self) -> list[object]:
        return legacy_api.list_elections(
            pace_seconds=self._pace_seconds,
            http_backend=self._http_backend,  # type: ignore[arg-type]
        )

    def fetch_election_rosters(
        self,
        election_id: str,
        *,
        pace_seconds: float | None = None,
        county_ids: list[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[list[CountyRoster], list[CountyFetchFailure]]:
        pace = pace_seconds if pace_seconds is not None else self._pace_seconds
        failures: list[CountyFetchFailure] = []
        all_rosters: list[CountyRoster] = []

        with LegacySession(
            pace_seconds=pace,
            http_backend=self._http_backend,  # type: ignore[arg-type]
        ) as session:
            ev_dates = session.prime_election(election_id)
            filter_ids = set(county_ids) if county_ids else None

            for ev in ev_dates:
                ev_date = ev.date
                date_label = ev_date.isoformat()
                if on_progress:
                    on_progress(f"[{ev_date}] Fetching counties...")

                try:
                    html = fetch_ev_details_html(session, election_id, ev_date)
                except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                    detail = format_fetch_error(exc)
                    failures.append(
                        CountyFetchFailure(
                            label=f"{date_label}: turnout HTML failed ({detail})",
                        ),
                    )
                    logger.warning(
                        "Turnout HTML fetch failed for election %s on %s: %s",
                        election_id,
                        date_label,
                        detail,
                    )
                    continue

                id_by_name = extract_county_ids(html)
                if not id_by_name:
                    failures.append(
                        CountyFetchFailure(
                            label=f"{date_label}: no county IDs in turnout HTML",
                        ),
                    )
                    logger.warning(
                        "No county IDs in turnout HTML for election %s on %s.",
                        election_id,
                        date_label,
                    )
                    continue

                county_names = {cid: name for name, cid in id_by_name.items()}
                target_ids = list(id_by_name.values())
                if filter_ids is not None:
                    target_ids = [cid for cid in target_ids if cid in filter_ids]
                    if not target_ids:
                        failures.append(
                            CountyFetchFailure(
                                label=f"{date_label}: no counties match --county-ids filter",
                            ),
                        )
                        continue

                date_rosters: list[CountyRoster] = []
                for county_id in target_ids:
                    county_label = county_names.get(county_id, county_id)
                    try:
                        county_rosters = fetch_roster_strategy_a(
                            session,
                            election_id,
                            ev_date,
                            [county_id],
                            pace_seconds=pace,
                            county_names=county_names,
                            skip_prime=True,
                        )
                        date_rosters.extend(county_rosters)
                    except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                        detail = format_fetch_error(exc)
                        failures.append(
                            CountyFetchFailure(
                                label=f"{county_label}/{date_label}: {detail}",
                            ),
                        )
                        logger.warning(
                            "County fetch failed for county_id=%s on %s: %s",
                            county_id,
                            date_label,
                            detail,
                        )

                if not date_rosters:
                    failures.append(
                        CountyFetchFailure(label=f"{date_label}: no county rosters fetched"),
                    )
                elif on_progress:
                    records = sum(r.total_voters for r in date_rosters)
                    on_progress(f"  done ({records:,} records)")

                all_rosters.extend(date_rosters)

        return all_rosters, failures

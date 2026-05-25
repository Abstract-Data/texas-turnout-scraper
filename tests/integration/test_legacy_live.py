"""Live integration tests for the legacy Texas SOS early-voting portal.

Skipped by default; pass ``--live`` to run against the real portal.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.integration._helpers import (
    LIVE_HTTP_ERRORS,
    is_retryable_http_error,
    skip_on_live_http_error,
)
from texas_turnout_scraper import legacy_api
from texas_turnout_scraper.elections import get_ev_dates
from texas_turnout_scraper.models import CountyRoster, CountyTurnout
from texas_turnout_scraper.session import LegacySession

_KNOWN_LEGACY_ELECTION_ID = "49664"
_LOVING_COUNTY_ID = "149"
_LOVING_COUNTY_NAME = "LOVING"
_RETRYABLE_STATUSES = {500, 502, 503, 504}


@pytest.mark.live
def test_legacy_list_elections() -> None:
    elections = legacy_api.list_elections()

    assert len(elections) >= 3
    assert all(isinstance(e.source_election_id, str) for e in elections)


@pytest.mark.live
def test_legacy_get_ev_dates() -> None:
    with LegacySession() as session:
        ev_dates = get_ev_dates(session, _KNOWN_LEGACY_ELECTION_ID)

    if not ev_dates:
        pytest.skip(f"No EV dates returned for election {_KNOWN_LEGACY_ELECTION_ID}")

    assert len(ev_dates) >= 1
    assert all(isinstance(d.date, date) for d in ev_dates)


@pytest.mark.live
def test_legacy_fetch_turnout() -> None:
    turnout: list[CountyTurnout] = []
    with LegacySession() as session:
        ev_dates = get_ev_dates(session, _KNOWN_LEGACY_ELECTION_ID)
        if not ev_dates:
            pytest.skip(f"No EV dates for election {_KNOWN_LEGACY_ELECTION_ID}")

        for ev_date_obj in ev_dates:
            try:
                rows = legacy_api.fetch_county_turnout(
                    _KNOWN_LEGACY_ELECTION_ID,
                    ev_date_obj.date,
                )
            except LIVE_HTTP_ERRORS as exc:
                if is_retryable_http_error(exc, _RETRYABLE_STATUSES):
                    continue
                skip_on_live_http_error(exc, context="fetch_county_turnout")
            if rows:
                turnout = rows
                break

    if not turnout:
        pytest.skip(
            f"fetch_county_turnout returned no rows for election {_KNOWN_LEGACY_ELECTION_ID}"
        )

    assert len(turnout) > 0
    counties = {row.county.upper() for row in turnout}
    assert "STATEWIDE" not in counties


@pytest.mark.live
def test_legacy_fetch_single_county_roster() -> None:
    roster: CountyRoster | None = None
    ev_date_used: date | None = None
    with LegacySession() as session:
        ev_dates = get_ev_dates(session, _KNOWN_LEGACY_ELECTION_ID)
        if not ev_dates:
            pytest.skip(f"No EV dates for election {_KNOWN_LEGACY_ELECTION_ID}")

        # Prefer mid-period dates (early dates often have empty small-county rosters).
        candidates = sorted({d.date for d in ev_dates}, reverse=True)[:10]
        for ev_date in candidates:
            try:
                roster = legacy_api.fetch_single_county_roster(
                    _KNOWN_LEGACY_ELECTION_ID,
                    ev_date,
                    county_id=_LOVING_COUNTY_ID,
                    county_name=_LOVING_COUNTY_NAME,
                )
            except LIVE_HTTP_ERRORS as exc:
                if is_retryable_http_error(exc, _RETRYABLE_STATUSES):
                    continue
                skip_on_live_http_error(exc, context="fetch_single_county_roster")
            except ValueError:
                continue
            if roster.records:
                ev_date_used = ev_date
                break

    if roster is None or ev_date_used is None or not roster.records:
        pytest.skip(
            f"No roster returned for {_LOVING_COUNTY_NAME} "
            f"(county_id={_LOVING_COUNTY_ID}) on {_KNOWN_LEGACY_ELECTION_ID}"
        )
    ev_date = ev_date_used

    assert roster.county
    assert roster.election_id == _KNOWN_LEGACY_ELECTION_ID
    assert roster.report_date == ev_date
    assert len(roster.records) > 0

    for record in roster.records:
        assert isinstance(record.id_voter, str)
        assert record.county
        assert record.election_id == _KNOWN_LEGACY_ELECTION_ID
        assert record.report_date == ev_date

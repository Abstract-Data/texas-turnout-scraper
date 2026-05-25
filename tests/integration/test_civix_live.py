"""Live integration tests for the Civix EVR API.

Skipped by default; pass ``--live`` to run against the real API.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from requests.exceptions import HTTPError as RequestsHTTPError

from tests.integration._helpers import skip_on_live_http_error
from texas_turnout_scraper.civix import CivixClient, fetch_county_roster
from texas_turnout_scraper.models import CivixCountyRef, CivixElection

_LIVE_HTTP_ERRORS = (httpx.HTTPStatusError, RequestsHTTPError)

_LOVING_COUNTY_NAME = "LOVING"


def _certified_with_ev_dates(elections: list[CivixElection]) -> list[CivixElection]:
    return [e for e in elections if e.certified and e.early_voting_dates]


def _loving_county(election: CivixElection) -> CivixCountyRef | None:
    for county in election.counties:
        if county.name.upper() == _LOVING_COUNTY_NAME:
            return county
    return None


def _county_with_roster(
    client: CivixClient,
    election: CivixElection,
    ev_date: date,
) -> CivixCountyRef | None:
    """Prefer LOVING; otherwise first county with roster_available on turnout."""
    loving = _loving_county(election)
    if loving is not None:
        return loving

    turnout = client.fetch_ev_turnout(int(election.source_election_id), ev_date)
    for row in turnout:
        if row.roster_available:
            return CivixCountyRef(county_id=row.county_id, name=row.county)
    return None


@pytest.mark.live
def test_civix_list_elections_returns_results() -> None:
    try:
        with CivixClient() as client:
            elections = client.list_elections()
    except _LIVE_HTTP_ERRORS as exc:
        skip_on_live_http_error(exc, context="list_elections")

    assert len(elections) > 0
    assert all(isinstance(e.source_election_id, str) for e in elections)
    assert any(e.certified for e in elections)


@pytest.mark.live
def test_civix_fetch_ev_turnout_returns_counties() -> None:
    try:
        with CivixClient() as client:
            elections = client.list_elections()
            certified = _certified_with_ev_dates(elections)
            if not certified:
                pytest.skip("No certified elections with EV dates available")

            election = certified[0]
            ev_date = election.early_voting_dates[0].date
            turnout = client.fetch_ev_turnout(int(election.source_election_id), ev_date)
    except _LIVE_HTTP_ERRORS as exc:
        skip_on_live_http_error(exc, context="fetch_ev_turnout")

    assert len(turnout) > 0
    assert all(isinstance(t.election_id, str) for t in turnout)


@pytest.mark.live
def test_civix_fetch_ev_roster_csv_for_small_county() -> None:
    try:
        with CivixClient() as client:
            elections = client.list_elections()
            certified = _certified_with_ev_dates(elections)
            if not certified:
                pytest.skip("No certified elections with EV dates available")

            election = certified[0]
            ev_date = election.early_voting_dates[0].date
            county = _county_with_roster(client, election, ev_date)
            if county is None:
                pytest.skip("No county with roster available for selected election/date")

            roster = fetch_county_roster(
                client,
                election_id=int(election.source_election_id),
                election_date=ev_date,
                county_name=county.name,
                county_id=county.county_id,
            )
    except _LIVE_HTTP_ERRORS as exc:
        skip_on_live_http_error(exc, context="fetch_ev_roster_csv")

    assert roster.county == county.name
    assert roster.election_id == election.source_election_id
    assert roster.report_date == ev_date
    assert len(roster.records) > 0

    for record in roster.records:
        assert isinstance(record.id_voter, str)
        assert record.county == county.name
        assert record.election_id == election.source_election_id
        assert record.report_date == ev_date


@pytest.mark.live
def test_civix_voter_record_fields_populated() -> None:
    try:
        with CivixClient() as client:
            elections = client.list_elections()
            certified = _certified_with_ev_dates(elections)
            if not certified:
                pytest.skip("No certified elections with EV dates available")

            election = certified[0]
            ev_date = election.early_voting_dates[0].date
            county = _county_with_roster(client, election, ev_date)
            if county is None:
                pytest.skip("No county with roster available for selected election/date")

            roster = fetch_county_roster(
                client,
                election_id=int(election.source_election_id),
                election_date=ev_date,
                county_name=county.name,
                county_id=county.county_id,
            )
    except _LIVE_HTTP_ERRORS as exc:
        skip_on_live_http_error(exc, context="voter_record_fields")

    assert len(roster.records) > 0
    record = roster.records[0]
    assert isinstance(record.id_voter, str)
    assert record.voting_method is not None
    assert record.precinct
    assert record.county == county.name
    assert record.election_id == election.source_election_id
    assert record.report_date == ev_date
    assert record.voter_name

"""Unit tests for legacy SOS modules — all HTTP mocked via respx."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from texas_turnout_scraper.elections import get_ev_dates, list_elections
from texas_turnout_scraper.enums import ElectionType, VoteMethod
from texas_turnout_scraper.legacy_forms import legacy_ev_form_fields
from texas_turnout_scraper.models import LegacyElection
from texas_turnout_scraper.roster import _parse_county_csv, fetch_roster_strategy_a
from texas_turnout_scraper.session import LegacySession
from texas_turnout_scraper.turnout import (
    _detect_column_map,
    extract_county_ids,
    fetch_turnout,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "early_voting"
BASE_URL = LegacySession.BASE_URL


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _legacy_session() -> LegacySession:
    """Session without __enter__ (no automatic establish)."""
    return LegacySession(pace_seconds=0.0, http_backend="httpx")


def test_legacy_ev_form_fields_turnout_shape() -> None:
    fields = legacy_ev_form_fields("49664", date(2024, 10, 21))
    assert fields["idElection"] == "49664"
    assert fields["selectedDate"] == "2024-10-21 00:00:00.0"
    assert fields["idTown"] == ""
    assert fields["earlyVoteFlag"] == "true"


def test_legacy_ev_form_fields_roster_includes_id_town() -> None:
    fields = legacy_ev_form_fields("49664", date(2024, 10, 21), id_town="149")
    assert fields["idTown"] == "149"


# ---------------------------------------------------------------------------
# turnout column detection
# ---------------------------------------------------------------------------


def test_detect_column_map_maps_county_and_registered_voters() -> None:
    from bs4 import BeautifulSoup

    html = """
    <table>
      <tr><th>COUNTY</th><th>REGISTERED VOTERS</th><th>IN PERSON</th><th>MAIL</th></tr>
      <tr><td>HARRIS</td><td>1,000</td><td>50</td><td>10</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    assert table is not None
    rows = table.find_all("tr")
    col_map = _detect_column_map(rows)
    assert col_map is not None
    assert "county" in col_map
    assert "registered_voters" in col_map


# ---------------------------------------------------------------------------
# elections.py
# ---------------------------------------------------------------------------


@respx.mock
def test_list_elections_parses_dropdown() -> None:
    html = _load_text("legacy_election_index.html")
    respx.get(f"{BASE_URL}/Elections/getElectionDetails.do").mock(
        return_value=httpx.Response(200, text=html)
    )

    session = _legacy_session()
    try:
        elections = list_elections(session)
    finally:
        session.close()

    assert len(elections) == 3
    first = elections[0]
    assert first.source_election_id == "49664"
    assert "GENERAL" in first.election_name
    for election in elections:
        assert isinstance(election.source_election_id, str)


@respx.mock
def test_get_ev_dates_parses_date_dropdown() -> None:
    html = _load_text("legacy_ev_dates_49664.html")
    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=html)
    )

    session = _legacy_session()
    try:
        ev_dates = get_ev_dates(session, "49664")
    finally:
        session.close()

    assert len(ev_dates) == 3
    assert [d.date for d in ev_dates] == [
        date(2024, 10, 21),
        date(2024, 10, 22),
        date(2024, 11, 1),
    ]


def test_election_type_inferred_from_name() -> None:
    general = LegacyElection(
        source_election_id="49664",
        election_name="2024 NOVEMBER 5TH GENERAL ELECTION",
    )
    republican = LegacyElection(
        source_election_id="47832",
        election_name="2024 MARCH 5TH REPUBLICAN PRIMARY",
    )
    democratic = LegacyElection(
        source_election_id="47831",
        election_name="2024 MARCH 5TH DEMOCRATIC PRIMARY",
    )

    assert general.election_type == ElectionType.GENERAL
    assert republican.election_type == ElectionType.PRIMARY
    assert democratic.election_type == ElectionType.PRIMARY


def _mock_ev_dates() -> None:
    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))
    )


@respx.mock
def test_fetch_turnout_parses_county_table() -> None:
    html = _load_text("legacy_ev_details_49664.html")
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=html)
    )

    session = _legacy_session()
    try:
        results = fetch_turnout(session, "49664", date(2024, 10, 21))
    finally:
        session.close()

    assert len(results) == 3
    counties = {r.county: r for r in results}
    assert "STATEWIDE" not in counties

    loving = counties["LOVING"]
    assert loving.registered_voters == 100
    assert loving.in_person_votes_on_date == 2

    harris = counties["HARRIS"]
    assert harris.total_in_person_votes == 88500
    assert harris.total_mail_votes == 1200


def test_extract_county_ids_from_html() -> None:
    html = _load_text("legacy_ev_details_49664.html")
    mapping = extract_county_ids(html)

    assert list(mapping.values()) == ["149", "101", "227"]
    assert mapping["LOVING"] == "149"
    assert mapping["HARRIS"] == "101"
    assert mapping["TRAVIS"] == "227"
    assert "STATEWIDE" not in mapping


def test_extract_county_ids_empty_html_returns_empty_dict() -> None:
    assert extract_county_ids("<html><body><table></table></body></html>") == {}
    assert extract_county_ids("") == {}


@respx.mock
def test_statewide_row_excluded_from_turnout() -> None:
    html = _load_text("legacy_ev_details_49664.html")
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=html)
    )

    session = _legacy_session()
    try:
        results = fetch_turnout(session, "49664", date(2024, 10, 21))
    finally:
        session.close()

    assert all(r.county != "STATEWIDE" for r in results)


# ---------------------------------------------------------------------------
# roster.py
# ---------------------------------------------------------------------------

_LOVING_CSV = _load_text("legacy_voter_info_loving.csv")
_EV_DATE = date(2024, 10, 21)
_ELECTION_ID = "49664"


def test_parse_county_csv_returns_voter_records() -> None:
    roster = _parse_county_csv(
        raw_text=_LOVING_CSV,
        county_id="149",
        county_name="LOVING",
        source_election_id=_ELECTION_ID,
        ev_date=_EV_DATE,
    )

    assert roster is not None
    assert len(roster.records) == 6
    assert all(isinstance(r.id_voter, str) for r in roster.records)
    assert roster.records[2].voting_method == VoteMethod.MAIL_IN
    for record in roster.records:
        assert record.county == "LOVING"
        assert record.election_id == _ELECTION_ID
        assert record.report_date == _EV_DATE


def test_parse_county_csv_voter_name_stored() -> None:
    roster = _parse_county_csv(
        raw_text=_LOVING_CSV,
        county_id="149",
        county_name="LOVING",
        source_election_id=_ELECTION_ID,
        ev_date=_EV_DATE,
    )
    assert roster is not None
    assert roster.records[0].voter_name == "DOE, LOVING A"


def test_parse_county_csv_id_voter_always_string() -> None:
    roster = _parse_county_csv(
        raw_text=_LOVING_CSV,
        county_id="149",
        county_name="LOVING",
        source_election_id=_ELECTION_ID,
        ev_date=_EV_DATE,
    )
    assert roster is not None
    for record in roster.records:
        assert isinstance(record.id_voter, str)
    assert roster.records[0].id_voter == "2000000001"


def test_parse_county_csv_zfills_short_vuid() -> None:
    csv_text = (
        '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n"DOE, TEST","12345","IN-PERSON","1"\n'
    )
    roster = _parse_county_csv(
        raw_text=csv_text,
        county_id="149",
        county_name="LOVING",
        source_election_id=_ELECTION_ID,
        ev_date=_EV_DATE,
    )

    assert roster is not None
    assert len(roster.records) == 1
    assert roster.records[0].id_voter == "0000012345"


def test_parse_county_csv_empty_returns_none() -> None:
    assert (
        _parse_county_csv(
            raw_text="",
            county_id="149",
            county_name="LOVING",
            source_election_id=_ELECTION_ID,
            ev_date=_EV_DATE,
        )
        is None
    )
    assert (
        _parse_county_csv(
            raw_text='"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n',
            county_id="149",
            county_name="LOVING",
            source_election_id=_ELECTION_ID,
            ev_date=_EV_DATE,
        )
        is None
    )


def test_parse_county_csv_malformed_skips_rows() -> None:
    csv_text = (
        '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n'
        '"DOE, GOOD","2000000001","IN-PERSON","1"\n'
        '"DOE, BAD","","IN-PERSON","1"\n'
    )
    roster = _parse_county_csv(
        raw_text=csv_text,
        county_id="149",
        county_name="LOVING",
        source_election_id=_ELECTION_ID,
        ev_date=_EV_DATE,
    )

    assert roster is not None
    assert len(roster.records) == 1
    assert isinstance(roster.records[0].id_voter, str)


@respx.mock
def test_fetch_roster_strategy_a_with_county_names() -> None:
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        return_value=httpx.Response(200, text=_LOVING_CSV)
    )

    session = _legacy_session()
    try:
        rosters = fetch_roster_strategy_a(
            session,
            _ELECTION_ID,
            _EV_DATE,
            ["149"],
            pace_seconds=0.0,
            county_names={"149": "LOVING"},
        )
    finally:
        session.close()

    assert len(rosters) == 1
    assert rosters[0].county == "LOVING"
    assert all(r.county == "LOVING" for r in rosters[0].records)


@respx.mock
def test_fetch_roster_strategy_a_fallback_county_name() -> None:
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        return_value=httpx.Response(200, text=_LOVING_CSV)
    )

    session = _legacy_session()
    try:
        rosters = fetch_roster_strategy_a(
            session,
            _ELECTION_ID,
            _EV_DATE,
            ["149"],
            pace_seconds=0.0,
        )
    finally:
        session.close()

    assert len(rosters) == 1
    assert rosters[0].county == "COUNTY_149"
    assert all(r.county == "COUNTY_149" for r in rosters[0].records)


@respx.mock
def test_fetch_roster_strategy_a_raises_on_partial_http_failure() -> None:
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        side_effect=[
            httpx.Response(200, text=_LOVING_CSV),
            httpx.Response(500, text="server error"),
        ]
    )

    session = _legacy_session()
    try:
        with pytest.raises(RuntimeError, match=r"1 of 2 counties failed"):
            fetch_roster_strategy_a(
                session,
                _ELECTION_ID,
                _EV_DATE,
                ["149", "101"],
                pace_seconds=0.0,
            )
    finally:
        session.close()


@respx.mock
def test_fetch_roster_strategy_a_raises_on_empty_200() -> None:
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        side_effect=[
            httpx.Response(200, text=_LOVING_CSV),
            httpx.Response(200, text=""),
        ]
    )

    session = _legacy_session()
    try:
        with pytest.raises(RuntimeError, match=r"1 of 2 counties failed"):
            fetch_roster_strategy_a(
                session,
                _ELECTION_ID,
                _EV_DATE,
                ["149", "101"],
                pace_seconds=0.0,
            )
    finally:
        session.close()


@respx.mock
def test_fetch_roster_strategy_a_raises_on_csv_parse_failure() -> None:
    _mock_ev_dates()
    header_only_csv = '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n'
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        side_effect=[
            httpx.Response(200, text=_LOVING_CSV),
            httpx.Response(200, text=header_only_csv),
        ]
    )

    session = _legacy_session()
    try:
        with pytest.raises(RuntimeError, match=r"1 of 2 counties failed"):
            fetch_roster_strategy_a(
                session,
                _ELECTION_ID,
                _EV_DATE,
                ["149", "101"],
                pace_seconds=0.0,
            )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# session.py
# ---------------------------------------------------------------------------


@respx.mock
def test_legacy_session_establishes_cookie() -> None:
    respx.get(f"{BASE_URL}/Elections/getElectionDetails.do").mock(
        return_value=httpx.Response(
            200,
            headers={"Set-Cookie": "JSESSIONID=abc123; Path=/"},
            text="<html></html>",
        )
    )

    session = LegacySession(pace_seconds=0.0, http_backend="httpx")
    try:
        session.establish()
        assert session._client.cookies.get("JSESSIONID") == "abc123"
    finally:
        session.close()


def test_legacy_session_pace_enforced() -> None:
    session = LegacySession(pace_seconds=0.1, http_backend="httpx")
    try:
        session._last_request_at = time.monotonic()
        start = time.monotonic()
        session.pace()
        session.pace()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.1
    finally:
        session.close()


@respx.mock
def test_prime_election_skips_redundant_post() -> None:
    html = _load_text("legacy_ev_dates_49664.html")
    route = respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=html)
    )

    session = _legacy_session()
    try:
        first = session.prime_election("49664")
        second = session.prime_election("49664")
    finally:
        session.close()

    assert route.call_count == 1
    assert first == second
    assert len(first) == 3


@respx.mock
def test_list_elections_reuses_establish_html() -> None:
    html = _load_text("legacy_election_index.html")
    route = respx.get(f"{BASE_URL}/Elections/getElectionDetails.do").mock(
        return_value=httpx.Response(200, text=html)
    )

    session = _legacy_session()
    try:
        session.establish()
        elections = list_elections(session)
    finally:
        session.close()

    assert route.call_count == 1
    assert len(elections) == 3
    assert elections[0].source_election_id == "49664"


@respx.mock
def test_establish_resets_priming_cache() -> None:
    ev_route = respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))
    )
    respx.get(f"{BASE_URL}/Elections/getElectionDetails.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_election_index.html"))
    )

    session = _legacy_session()
    try:
        session.prime_election("49664")
        session.establish()
        session.prime_election("49664")
    finally:
        session.close()

    assert ev_route.call_count == 2

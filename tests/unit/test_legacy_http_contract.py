"""POST-body contract tests for the legacy SOS Struts portal."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx

from texas_turnout_scraper.legacy_forms import legacy_ev_form_fields
from texas_turnout_scraper.session import LegacySession
from texas_turnout_scraper.turnout import fetch_ev_details_html, fetch_turnout

FIXTURES = Path(__file__).parent.parent / "fixtures" / "early_voting"
BASE_URL = LegacySession.BASE_URL
_ELECTION_ID = "49664"
_EV_DATE = date(2024, 10, 21)


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _legacy_session() -> LegacySession:
    return LegacySession(pace_seconds=0.0, http_backend="httpx")


@respx.mock
def test_prime_election_posts_before_ev_details() -> None:
    call_order: list[str] = []

    def ev_dates_handler(request: httpx.Request) -> httpx.Response:
        call_order.append("ev_dates")
        assert request.url.path.endswith("/Elections/getElectionEVDates.do")
        body = request.content.decode()
        assert f"idElection={_ELECTION_ID}" in body.replace("+", " ")
        return httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))

    def ev_details_handler(request: httpx.Request) -> httpx.Response:
        call_order.append("ev_details")
        body = request.content.decode()
        assert "selectedDate=2024-10-21+00%3A00%3A00.0" in body or (
            "selectedDate=2024-10-21 00:00:00.0" in body
        )
        assert "earlyVoteFlag=true" in body
        assert "idTown=" in body
        return httpx.Response(200, text=_load_text("legacy_ev_details_49664.html"))

    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(side_effect=ev_dates_handler)
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(side_effect=ev_details_handler)

    session = _legacy_session()
    try:
        fetch_turnout(session, _ELECTION_ID, _EV_DATE)
    finally:
        session.close()

    assert call_order == ["ev_dates", "ev_details"]


@respx.mock
def test_roster_post_includes_id_town() -> None:
    captured: dict[str, str] = {}

    def roster_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, text=_load_text("legacy_voter_info_loving.csv"))

    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))
    )
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        side_effect=roster_handler
    )

    session = _legacy_session()
    try:
        session.prime_election(_ELECTION_ID)
        session._post_form(
            "/Elections/downloadVoterInfoReport.do",
            legacy_ev_form_fields(_ELECTION_ID, _EV_DATE, id_town="149"),
        )
    finally:
        session.close()

    body = captured["body"]
    assert "idTown=149" in body
    assert "earlyVoteFlag=true" in body
    assert "selectedDate=2024-10-21" in body


@respx.mock
def test_fetch_ev_details_html_returns_fixture_html() -> None:
    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))
    )
    expected = _load_text("legacy_ev_details_49664.html")
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=expected)
    )

    session = _legacy_session()
    try:
        html = fetch_ev_details_html(session, _ELECTION_ID, _EV_DATE)
    finally:
        session.close()

    assert html == expected

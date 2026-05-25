"""Unit tests for legacy Strategy B bulk ZIP streaming."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import respx

from texas_turnout_scraper.roster import fetch_roster_strategy_b
from texas_turnout_scraper.session import LegacySession

BASE_URL = LegacySession.BASE_URL
_ELECTION_ID = "49664"
_EV_DATE = date(2024, 10, 21)
_ZIP_BYTES = b"PK\x03\x04" + b"minimal-zip-content"
_EV_DATES_HTML = (
    Path(__file__).parent.parent / "fixtures" / "early_voting" / "legacy_ev_dates_49664.html"
).read_text()


def _mock_ev_dates() -> None:
    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_EV_DATES_HTML)
    )


@respx.mock
def test_fetch_roster_strategy_b_writes_streamed_zip(tmp_path: Path) -> None:
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadParticipationCountReport.do").mock(
        return_value=httpx.Response(200, content=_ZIP_BYTES)
    )

    session = LegacySession(pace_seconds=0.0, http_backend="httpx")
    try:
        zip_path = fetch_roster_strategy_b(session, _ELECTION_ID, _EV_DATE, tmp_path)
    finally:
        session.close()

    assert zip_path.exists()
    assert zip_path.read_bytes() == _ZIP_BYTES
    assert zip_path.name == "roster_2024-10-21_bulk.zip"


@respx.mock
def test_fetch_roster_strategy_b_primes_ev_dates(tmp_path: Path) -> None:
    ev_dates_route = respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_EV_DATES_HTML)
    )
    respx.post(f"{BASE_URL}/Elections/downloadParticipationCountReport.do").mock(
        return_value=httpx.Response(200, content=_ZIP_BYTES)
    )

    session = LegacySession(pace_seconds=0.0, http_backend="httpx")
    try:
        fetch_roster_strategy_b(session, _ELECTION_ID, _EV_DATE, tmp_path)
    finally:
        session.close()

    assert ev_dates_route.called


@respx.mock
def test_fetch_roster_strategy_b_updates_last_request_at(tmp_path: Path) -> None:
    _mock_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadParticipationCountReport.do").mock(
        return_value=httpx.Response(200, content=_ZIP_BYTES)
    )

    session = LegacySession(pace_seconds=0.0, http_backend="httpx")
    try:
        before = session._last_request_at
        fetch_roster_strategy_b(session, _ELECTION_ID, _EV_DATE, tmp_path)
        after = session._last_request_at
    finally:
        session.close()

    assert after > before


def test_fetch_roster_strategy_b_cloudscraper_stream(tmp_path: Path) -> None:
    mock_response = MagicMock()
    mock_response.iter_content.return_value = [_ZIP_BYTES[:4], _ZIP_BYTES[4:], b""]

    mock_scraper = MagicMock()
    mock_scraper.request.return_value = mock_response
    mock_scraper.headers = {}

    with patch("cloudscraper.create_scraper", return_value=mock_scraper):
        session = LegacySession(pace_seconds=0.0, http_backend="cloudscraper")
        try:
            with patch.object(session, "prime_election", return_value=[]):
                zip_path = fetch_roster_strategy_b(session, _ELECTION_ID, _EV_DATE, tmp_path)
        finally:
            session.close()

    assert zip_path.read_bytes() == _ZIP_BYTES
    mock_scraper.request.assert_called_once()

"""Unit tests for RosterSource adapters (CivixSource, LegacySource)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import CountyRoster, VoterRecord
from texas_turnout_scraper.sources import CivixSource, CountyFetchFailure, LegacySource


def _civix_election() -> MagicMock:
    election = MagicMock()
    election.id = 53813
    election.source_election_id = "53813"
    ev = MagicMock()
    ev.date = date(2026, 2, 17)
    election.early_voting_dates = [ev]
    return election


def test_civix_source_resolves_election_by_source_election_id() -> None:
    election = _civix_election()
    turnout_row = MagicMock(county="HARRIS", county_id=1, roster_available=True)
    roster = CountyRoster(
        county="HARRIS",
        county_id=1,
        election_id="53813",
        report_date=date(2026, 2, 17),
        source="civix",
        records=[
            VoterRecord(
                id_voter="0123456789",
                voting_method=VoteMethod.IN_PERSON,
                precinct="100",
                county="HARRIS",
                election_id="53813",
                report_date=date(2026, 2, 17),
            )
        ],
    )

    with (
        patch("texas_turnout_scraper.sources.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.sources.fetch_county_roster") as mock_fetch,
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [turnout_row]
        mock_client_cls.return_value = mock_client
        mock_fetch.return_value = roster

        source = CivixSource(http_backend="httpx")
        rosters, failures = source.fetch_election_rosters("53813", pace_seconds=0.0)

    assert failures == []
    assert len(rosters) == 1
    mock_client.fetch_ev_turnout.assert_called_once_with(
        election_id=53813,
        election_date=date(2026, 2, 17),
    )


def test_civix_source_records_county_failure_label() -> None:
    election = _civix_election()
    turnout_row = MagicMock(county="HARRIS", county_id=1, roster_available=True)

    with (
        patch("texas_turnout_scraper.sources.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.sources.fetch_county_roster") as mock_fetch,
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [turnout_row]
        mock_client_cls.return_value = mock_client
        mock_fetch.side_effect = RuntimeError("county failed")

        source = CivixSource(http_backend="httpx")
        _rosters, failures = source.fetch_election_rosters("53813", pace_seconds=0.0)

    assert len(failures) == 2
    assert any("HARRIS" in f.label for f in failures)
    assert any("no county rosters fetched" in f.label for f in failures)


def test_legacy_source_continues_after_turnout_http_error() -> None:
    with (
        patch("texas_turnout_scraper.sources.LegacySession") as mock_sess_cls,
        patch("texas_turnout_scraper.sources.fetch_ev_details_html") as mock_html,
    ):
        mock_sess = MagicMock()
        mock_sess.__enter__.return_value = mock_sess
        mock_sess.__exit__.return_value = None
        mock_sess.prime_election.return_value = [
            MagicMock(date=date(2024, 10, 21)),
            MagicMock(date=date(2024, 10, 22)),
        ]
        mock_sess_cls.return_value = mock_sess
        mock_html.side_effect = httpx.HTTPStatusError(
            "502 Server Error",
            request=MagicMock(),
            response=MagicMock(status_code=502),
        )

        source = LegacySource(http_backend="httpx")
        rosters, failures = source.fetch_election_rosters("49664", pace_seconds=0.0)

    assert rosters == []
    assert len(failures) == 2
    assert all(isinstance(f, CountyFetchFailure) for f in failures)
    assert all("turnout HTML failed" in f.label for f in failures)


def test_civix_source_missing_election_raises_value_error() -> None:
    with patch("texas_turnout_scraper.sources.CivixClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = []
        mock_client_cls.return_value = mock_client

        source = CivixSource(http_backend="httpx")
        with pytest.raises(ValueError, match="not found"):
            source.fetch_election_rosters("99999", pace_seconds=0.0)

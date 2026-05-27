"""Tests for fetch-all commands, audit paths, and partial-fetch exit behavior."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from requests.exceptions import HTTPError as RequestsHTTPError
from typer.testing import CliRunner

from texas_turnout_scraper.cli import (
    _exit_on_partial_fetch_failures,
    audit_run,
    civix_fetch_all,
)
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.mcp_server import run_audit as mcp_run_audit
from texas_turnout_scraper.models import (
    CivixCountyTurnout,
    CivixElection,
    CivixElectionDate,
    CountyRoster,
    VoterRecord,
)
from texas_turnout_scraper.writer import (
    read_turnout_csv,
    stored_ed_turnout_path,
    stored_roster_ev_path,
    write_roster_csv,
)

runner = CliRunner()


def _civix_election() -> CivixElection:
    return CivixElection(
        source_election_id="53813",
        id=53813,
        type="EV",
        election_date=date(2026, 3, 3),
        election_name="2026 REPUBLICAN PRIMARY ELECTION",
        certified=True,
        early_voting_dates=[CivixElectionDate(date=date(2026, 2, 17), date_turnout_id=1)],
        counties=[],
    )


def _minimal_roster(
    path: Path, *, election_id: str = "53813", report_date: date | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_roster_csv(
        [
            VoterRecord(
                id_voter="0123456789",
                voting_method=VoteMethod.IN_PERSON,
                precinct="100",
                county="HARRIS",
                election_id=election_id,
                report_date=report_date or date(2026, 2, 17),
            )
        ],
        path,
    )


def test_exit_on_partial_fetch_failures_raises() -> None:
    with pytest.raises(typer.Exit) as exc_info:
        _exit_on_partial_fetch_failures(["HARRIS/2026-02-17: HTTPError"])
    assert exc_info.value.exit_code == 1


def test_civix_fetch_all_dry_run_does_not_fetch_turnout() -> None:
    election = _civix_election()

    with patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client_cls.return_value = mock_client

        civix_fetch_all(
            election_id="53813",
            dry_run=True,
        )
    mock_client.fetch_ev_turnout.assert_not_called()


def test_civix_fetch_all_exits_on_partial_county_failure(tmp_path: Path) -> None:
    election = _civix_election()
    turnout_row_ok = CivixCountyTurnout(
        election_id="53813",
        report_date=date(2026, 2, 17),
        county="HARRIS",
        county_id=1,
        registered_voters=10000,
        in_person_votes_on_date=100,
        total_in_person_votes=100,
        total_mail_votes=0,
        roster_available=True,
    )
    turnout_row_fail = CivixCountyTurnout(
        election_id="53813",
        report_date=date(2026, 2, 17),
        county="DALLAS",
        county_id=2,
        registered_voters=10000,
        in_person_votes_on_date=100,
        total_in_person_votes=100,
        total_mail_votes=0,
        roster_available=True,
    )
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
        patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.civix.fetch_county_roster") as mock_fetch,
        patch("texas_turnout_scraper.cli._update_election_index"),
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [turnout_row_ok, turnout_row_fail]
        mock_client_cls.return_value = mock_client
        mock_fetch.side_effect = [roster, RuntimeError("county failed")]

        with pytest.raises(typer.Exit) as exc_info:
            civix_fetch_all(
                election_id="53813",
                output_dir=tmp_path / "civix",
                index_path=tmp_path / "index.json",
            )

    assert exc_info.value.exit_code == 1
    assert (tmp_path / "civix" / "53813" / "roster_ev_53813.csv").exists()


def test_civix_fetch_all_continues_on_requests_http_error(tmp_path: Path) -> None:
    election = _civix_election()
    turnout_row_ok = CivixCountyTurnout(
        election_id="53813",
        report_date=date(2026, 2, 17),
        county="HARRIS",
        county_id=1,
        registered_voters=10000,
        in_person_votes_on_date=100,
        total_in_person_votes=100,
        total_mail_votes=0,
        roster_available=True,
    )
    turnout_row_fail = CivixCountyTurnout(
        election_id="53813",
        report_date=date(2026, 2, 17),
        county="LAMPASAS",
        county_id=141,
        registered_voters=10000,
        in_person_votes_on_date=100,
        total_in_person_votes=100,
        total_mail_votes=0,
        roster_available=True,
    )
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
    response = MagicMock(status_code=502)
    http_error = RequestsHTTPError("502 Server Error", response=response)

    with (
        patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.civix.fetch_county_roster") as mock_fetch,
        patch("texas_turnout_scraper.cli._update_election_index"),
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [turnout_row_ok, turnout_row_fail]
        mock_client_cls.return_value = mock_client
        mock_fetch.side_effect = [roster, http_error]

        with pytest.raises(typer.Exit) as exc_info:
            civix_fetch_all(
                election_id="53813",
                output_dir=tmp_path / "civix",
                index_path=tmp_path / "index.json",
            )

    assert exc_info.value.exit_code == 1
    assert (tmp_path / "civix" / "53813" / "roster_ev_53813.csv").exists()


def _civix_ev_turnout_row(county: str, county_id: int) -> CivixCountyTurnout:
    return CivixCountyTurnout(
        election_id="53813",
        report_date=date(2026, 2, 17),
        county=county,
        county_id=county_id,
        registered_voters=10000,
        in_person_votes_on_date=100,
        total_in_person_votes=100,
        total_mail_votes=0,
        roster_available=True,
    )


def _civix_ed_turnout_row(county: str, county_id: int) -> CivixCountyTurnout:
    return CivixCountyTurnout(
        election_id="53813",
        report_date=date(2026, 3, 3),
        county=county,
        county_id=county_id,
        registered_voters=10000,
        in_person_votes_on_date=2500,
        total_in_person_votes=3500,
        total_mail_votes=150,
        roster_available=True,
    )


def _civix_roster(county: str, county_id: int) -> CountyRoster:
    return CountyRoster(
        county=county,
        county_id=county_id,
        election_id="53813",
        report_date=date(2026, 2, 17),
        source="civix",
        records=[
            VoterRecord(
                id_voter="0123456789",
                voting_method=VoteMethod.IN_PERSON,
                precinct="100",
                county=county,
                election_id="53813",
                report_date=date(2026, 2, 17),
            )
        ],
    )


def test_civix_fetch_all_writes_election_day_turnout(tmp_path: Path) -> None:
    election = _civix_election()
    ev_row = _civix_ev_turnout_row("HARRIS", 1)
    ed_rows = [
        _civix_ed_turnout_row("HARRIS", 1),
        _civix_ed_turnout_row("DALLAS", 2),
    ]

    with (
        patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.civix.fetch_county_roster") as mock_fetch,
        patch("texas_turnout_scraper.cli._update_election_index"),
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [ev_row]
        mock_client.fetch_ed_turnout.return_value = ed_rows
        mock_client_cls.return_value = mock_client
        mock_fetch.return_value = _civix_roster("HARRIS", 1)

        civix_fetch_all(
            election_id="53813",
            output_dir=tmp_path / "civix",
            index_path=tmp_path / "index.json",
        )

    mock_client.fetch_ed_turnout.assert_called_once_with(
        election_id=53813,
        election_date=date(2026, 3, 3),
    )

    ed_path = stored_ed_turnout_path(tmp_path / "civix", "53813", date(2026, 3, 3))
    assert ed_path.exists()
    written = read_turnout_csv(ed_path)
    assert [r.county for r in written] == ["HARRIS", "DALLAS"]
    assert written[0].report_date == date(2026, 3, 3)
    assert written[0].in_person_votes_on_date == 2500


def test_civix_fetch_all_tolerates_election_day_turnout_failure(tmp_path: Path) -> None:
    election = _civix_election()
    ev_row = _civix_ev_turnout_row("HARRIS", 1)
    response = MagicMock(status_code=404)

    with (
        patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.civix.fetch_county_roster") as mock_fetch,
        patch("texas_turnout_scraper.cli._update_election_index"),
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [ev_row]
        mock_client.fetch_ed_turnout.side_effect = RequestsHTTPError(
            "404 Not Found", response=response
        )
        mock_client_cls.return_value = mock_client
        mock_fetch.return_value = _civix_roster("HARRIS", 1)

        civix_fetch_all(
            election_id="53813",
            output_dir=tmp_path / "civix",
            index_path=tmp_path / "index.json",
        )

    ed_path = stored_ed_turnout_path(tmp_path / "civix", "53813", date(2026, 3, 3))
    assert not ed_path.exists()
    assert (tmp_path / "civix" / "53813" / "roster_ev_53813.csv").exists()


def test_civix_fetch_all_skips_election_day_turnout_when_empty(tmp_path: Path) -> None:
    election = _civix_election()
    ev_row = _civix_ev_turnout_row("HARRIS", 1)

    with (
        patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls,
        patch("texas_turnout_scraper.civix.fetch_county_roster") as mock_fetch,
        patch("texas_turnout_scraper.cli._update_election_index"),
    ):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client.list_elections.return_value = [election]
        mock_client.fetch_ev_turnout.return_value = [ev_row]
        mock_client.fetch_ed_turnout.return_value = []
        mock_client_cls.return_value = mock_client
        mock_fetch.return_value = _civix_roster("HARRIS", 1)

        civix_fetch_all(
            election_id="53813",
            output_dir=tmp_path / "civix",
            index_path=tmp_path / "index.json",
        )

    ed_path = stored_ed_turnout_path(tmp_path / "civix", "53813", date(2026, 3, 3))
    assert not ed_path.exists()


def test_audit_run_uses_combined_roster_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = stored_roster_ev_path(tmp_path, "civix", "53813")
    _minimal_roster(roster_path)

    audit_run(
        election_id="53813",
        ev_date=None,
        source="civix",
        data_dir=tmp_path,
        output="json",
    )

    captured = capsys.readouterr()
    assert "53813" in captured.out
    assert (tmp_path / "elections" / "civix" / "53813" / "audit_ev_53813.json").exists()


def test_mcp_run_audit_uses_combined_roster_path(tmp_path: Path) -> None:
    roster_path = stored_roster_ev_path(tmp_path, "legacy", "49664")
    _minimal_roster(
        roster_path,
        election_id="49664",
        report_date=date(2024, 10, 21),
    )

    report = mcp_run_audit(
        election_id="49664",
        ev_date="",
        source="legacy",
        data_dir=str(tmp_path),
    )

    assert "error" not in report
    assert report["election_id"] == "49664"
    assert report["report_date"] == "2024-10-21"

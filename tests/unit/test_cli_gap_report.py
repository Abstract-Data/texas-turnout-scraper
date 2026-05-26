"""CLI tests for civix gap-report."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from texas_turnout_scraper.cli import app
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import (
    CivixElection,
    CivixElectionDate,
    CountyTurnout,
    VoterRecord,
)
from texas_turnout_scraper.writer import write_roster_csv, write_turnout_csv

runner = CliRunner()


def _election() -> CivixElection:
    return CivixElection(
        source_election_id="58315",
        id=58315,
        type="EV",
        election_date=date(2026, 5, 26),
        election_name="2026 REPUBLICAN PRIMARY RUNOFF ELECTION",
        certified=False,
        early_voting_dates=[CivixElectionDate(date=date(2026, 5, 22), date_turnout_id=1)],
        counties=[],
    )


def test_civix_gap_report_uses_stored_turnout(tmp_path: Path) -> None:
    out_dir = tmp_path / "data" / "elections" / "civix"
    election_dir = out_dir / "58315"
    election_dir.mkdir(parents=True)

    write_roster_csv(
        [
            VoterRecord(
                id_voter="0000000001",
                voter_name="TEST, VOTER",
                voting_method=VoteMethod.IN_PERSON,
                precinct="100",
                county="HARRIS",
                election_id="58315",
                report_date=date(2026, 5, 22),
            )
        ],
        election_dir / "roster_ev_58315.csv",
    )
    write_turnout_csv(
        [
            CountyTurnout(
                election_id="58315",
                report_date=date(2026, 5, 22),
                county="HARRIS",
                county_id=101,
                registered_voters=1000,
                in_person_votes_on_date=1,
                total_in_person_votes=100,
                total_mail_votes=25,
                roster_available=True,
                source="civix",
            )
        ],
        election_dir / "turnout_ev_2026-05-22.csv",
    )

    with patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.list_elections.return_value = [_election()]

        result = runner.invoke(
            app,
            [
                "civix",
                "gap-report",
                "58315",
                "--output-dir",
                str(out_dir),
                "--turnout-source",
                "stored",
                "--no-write-files",
            ],
        )

    assert result.exit_code == 0
    assert "Turnout vs Roster Gap" in result.stdout
    assert "125" in result.stdout.replace(",", "")
    assert "Gap" in result.stdout

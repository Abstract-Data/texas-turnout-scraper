"""Tests for civix elections list sorting and interactive dispatch."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from typer.testing import CliRunner

from texas_turnout_scraper.cli import (
    _sort_civix_elections,
    _sort_civix_ev_dates,
    app,
)
from texas_turnout_scraper.models import CivixElection, CivixElectionDate

runner = CliRunner()


def _election(
    *,
    election_id: int,
    election_date: date,
    ev_date: date | None = None,
) -> CivixElection:
    ev = ev_date or election_date
    return CivixElection(
        source_election_id=str(election_id),
        id=election_id,
        type="EV",
        election_date=election_date,
        election_name=f"Election {election_id}",
        certified=True,
        early_voting_dates=[CivixElectionDate(date=ev, date_turnout_id=1)],
        counties=[],
    )


def test_sort_civix_elections_newest_first() -> None:
    older = _election(election_id=1, election_date=date(2024, 1, 1))
    newer = _election(election_id=2, election_date=date(2026, 3, 3))
    sorted_elections = _sort_civix_elections([older, newer])
    assert [e.id for e in sorted_elections] == [2, 1]


def test_sort_civix_ev_dates_newest_first() -> None:
    election = _election(election_id=99, election_date=date(2026, 3, 3))
    election.early_voting_dates = [
        CivixElectionDate(date=date(2026, 2, 10), date_turnout_id=1),
        CivixElectionDate(date=date(2026, 2, 28), date_turnout_id=2),
    ]
    sorted_dates = _sort_civix_ev_dates(election)
    assert [d.date for d in sorted_dates] == [date(2026, 2, 28), date(2026, 2, 10)]


def test_civix_elections_table_sorted_without_interactive() -> None:
    older = _election(election_id=100, election_date=date(2024, 5, 1))
    newer = _election(election_id=200, election_date=date(2026, 5, 1))

    with patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.list_elections.return_value = [older, newer]

        result = runner.invoke(app, ["civix", "elections", "--no-interactive"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("200")]
    assert lines, "expected newer election row in output"
    assert result.stdout.index("200") < result.stdout.index("100")


def test_civix_elections_interactive_skips_table_before_prompt() -> None:
    election = _election(election_id=53813, election_date=date(2026, 3, 3))

    with patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.list_elections.return_value = [election]

        with patch(
            "texas_turnout_scraper.cli._common._prompt_select",
            side_effect=["53813", "done"],
        ):
            result = runner.invoke(
                app,
                ["civix", "elections", "--interactive"],
            )

    assert result.exit_code == 0
    assert "EV DATES" not in result.stdout
    assert "53813" in result.stdout


def test_civix_elections_interactive_dispatches_turnout() -> None:
    election = _election(election_id=53813, election_date=date(2026, 3, 3))

    with patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.list_elections.return_value = [election]

        with patch(
            "texas_turnout_scraper.cli._common._prompt_select",
            side_effect=["53813", "turnout", "2026-03-03"],
        ):
            with patch(
                "texas_turnout_scraper.cli.civix.civix_turnout_fetch",
            ) as mock_turnout:
                result = runner.invoke(
                    app,
                    ["civix", "elections", "--interactive"],
                )

    assert result.exit_code == 0
    mock_turnout.assert_called_once()
    call_kwargs = mock_turnout.call_args.kwargs
    assert call_kwargs["election_id"] == "53813"
    assert call_kwargs["ev_date"] == "2026-03-03"

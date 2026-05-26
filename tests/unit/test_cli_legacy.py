"""CLI tests for legacy subcommands."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from texas_turnout_scraper.cli import (
    _exit_on_legacy_api_error,
    legacy_roster_fetch,
    legacy_turnout_fetch,
)

_EV_DATE = date(2024, 10, 21)


def test_exit_on_legacy_api_error_writes_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        _exit_on_legacy_api_error(
            ValueError("No county IDs found in turnout HTML for election 49664 on 2024-10-21")
        )

    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "No county IDs found" in captured.err
    assert "300000000" not in captured.err


def test_legacy_roster_fetch_exits_on_value_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "texas_turnout_scraper.legacy_api.fetch_roster",
        side_effect=ValueError(
            "No county IDs found in turnout HTML for election 49664 on 2024-10-21"
        ),
    ):
        with pytest.raises(typer.Exit) as exc_info:
            legacy_roster_fetch("49664", _EV_DATE)

    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "No county IDs found" in captured.err


def test_legacy_turnout_fetch_exits_on_runtime_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "texas_turnout_scraper.legacy_api.fetch_county_turnout",
        side_effect=RuntimeError("2 of 3 counties failed for election 49664 on 2024-10-21"),
    ):
        with pytest.raises(typer.Exit) as exc_info:
            legacy_turnout_fetch("49664", _EV_DATE)

    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "counties failed" in captured.err


def test_legacy_list_elections_mcp_omits_ev_dates_count() -> None:
    """MCP legacy_list_elections must not expose misleading ev_dates_count on list."""
    mcp_source = (
        Path(__file__).resolve().parents[2] / "src" / "texas_turnout_scraper" / "mcp_server.py"
    ).read_text()
    fn_block = mcp_source.split("def legacy_list_elections")[1].split("@mcp.tool()")[0]

    assert "ev_dates_count" not in fn_block
    assert "election_year" in fn_block

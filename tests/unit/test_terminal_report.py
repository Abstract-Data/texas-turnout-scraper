"""Unit tests for Rich terminal report rendering."""

from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path

from rich.console import Console

from texas_turnout_scraper import terminal_report
from texas_turnout_scraper.models import (
    AuditFinding,
    CountyTurnoutRosterGap,
    TurnoutRosterGapReport,
    VoterfileMatchReport,
)


def test_print_gap_report_summary_renders_key_labels(monkeypatch) -> None:
    buffer = StringIO()
    monkeypatch.setattr(terminal_report, "_console", Console(file=buffer, highlight=False))

    report = TurnoutRosterGapReport(
        election_id="58315",
        election_name="TEST RUNOFF",
        ev_date=date(2026, 5, 22),
        roster_path="roster_ev_58315.csv",
        turnout_source="stored",
        roster_row_count=100,
        roster_unique_vuids=100,
        counties=[
            CountyTurnoutRosterGap(
                county="HARRIS",
                turnout_total=100,
                roster_total=90,
                gap_total=10,
                gap_pct=0.1,
            )
        ],
        turnout_total=100,
        roster_total=90,
        gap_total=10,
        gap_pct=0.1,
        counties_with_gap=1,
    )

    terminal_report.print_gap_report_summary(report)
    output = buffer.getvalue()
    assert "Turnout vs Roster Gap" in output
    assert "Turnout (online)" in output
    assert "HARRIS" in output


def test_print_voterfile_match_summary_renders_match_panel(monkeypatch) -> None:
    buffer = StringIO()
    monkeypatch.setattr(terminal_report, "_console", Console(file=buffer, highlight=False))

    report = VoterfileMatchReport(
        election_id="58315",
        report_date=date(2026, 5, 22),
        voterfile_path="vf.csv",
        roster_path="roster.csv",
        total_roster_records=100,
        matched_count=95,
        unmatched_count=5,
        match_rate=0.95,
        findings=[
            AuditFinding(
                finding_type="unmatched_voters",
                detail="5 unmatched",
                severity="warning",
            )
        ],
    )

    terminal_report.print_voterfile_match_summary(
        report,
        report_path=Path("match_report.json"),
    )
    output = buffer.getvalue()
    assert "Match Summary" in output
    assert "Matched" in output
    assert "Audit Findings" in output

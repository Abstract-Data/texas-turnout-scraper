"""CLI smoke tests for tx-turnout voterfile commands."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from texas_turnout_scraper.cli import app
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import VoterRecord
from texas_turnout_scraper.writer import write_roster_csv

runner = CliRunner()

FIXTURE_VOTERFILE = (
    Path(__file__).parent.parent / "fixtures" / "voterfiles" / "sample_voterfile.csv"
)


def _write_minimal_roster(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_roster_csv(
        [
            VoterRecord(
                id_voter="0000000001",
                voting_method=VoteMethod.IN_PERSON,
                precinct="510",
                county="HARRIS",
                election_id="12345",
                report_date=date(2026, 5, 20),
            )
        ],
        path,
    )


def test_voterfile_detect_columns_lists_vuid(tmp_path: Path) -> None:
    vf = tmp_path / "vf.csv"
    vf.write_text('"VUID","COUNTY","PCT"\n"1","HARRIS","0510"\n', encoding="utf-8")
    result = runner.invoke(app, ["voterfile", "detect-columns", str(vf)])
    assert result.exit_code == 0
    assert "VUID" in result.stdout


def test_voterfile_detect_columns_missing_file_exits_one() -> None:
    result = runner.invoke(app, ["voterfile", "detect-columns", "/no/such/voterfile.csv"])
    assert result.exit_code == 1
    assert "not found" in result.stdout.lower() or "not found" in (result.stderr or "").lower()


def test_voterfile_match_no_interactive_writes_outputs(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    _write_minimal_roster(roster)
    result = runner.invoke(
        app,
        [
            "voterfile",
            "match",
            str(roster),
            str(FIXTURE_VOTERFILE),
            "--no-interactive",
            "--output-dir",
            str(tmp_path),
            "--no-save-mapping",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "matched_roster.csv").exists()
    report_path = tmp_path / "match_report_roster.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["matched_count"] == 1
    assert report["total_voterfile_records"] == 10


def test_voterfile_match_report_only_skips_enriched_csv(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    _write_minimal_roster(roster)
    result = runner.invoke(
        app,
        [
            "voterfile",
            "match",
            str(roster),
            str(FIXTURE_VOTERFILE),
            "--no-interactive",
            "--report-only",
            "--output-dir",
            str(tmp_path),
            "--no-save-mapping",
        ],
    )
    assert result.exit_code == 0
    assert not (tmp_path / "matched_roster.csv").exists()
    assert (tmp_path / "match_report_roster.json").exists()


def test_voterfile_match_saves_mapping_sidecar(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    _write_minimal_roster(roster)
    vf_copy = tmp_path / "voterfile.csv"
    vf_copy.write_text(FIXTURE_VOTERFILE.read_text(encoding="utf-8"), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "voterfile",
            "match",
            str(roster),
            str(vf_copy),
            "--no-interactive",
            "--output-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    sidecar = tmp_path / "voterfile.mapping.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["vuid"] == "VUID"

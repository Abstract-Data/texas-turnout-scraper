"""CLI smoke tests for tx-turnout voterfile commands."""

from __future__ import annotations

import json
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
            "--count-voterfile",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "matched_roster.csv").exists()
    report_path = tmp_path / "match_report_roster.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["matched_count"] == 1
    assert report["total_voterfile_records"] == 10


def test_voterfile_match_default_skips_voterfile_row_count(tmp_path: Path) -> None:
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
    assert result.exit_code == 0
    report = json.loads((tmp_path / "match_report_roster.json").read_text(encoding="utf-8"))
    assert report["total_voterfile_records"] is None


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


def test_voterfile_redetect_ignores_existing_sidecar(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    _write_minimal_roster(roster)
    vf_copy = tmp_path / "voterfile.csv"
    vf_copy.write_text(FIXTURE_VOTERFILE.read_text(encoding="utf-8"), encoding="utf-8")
    sidecar = tmp_path / "voterfile.mapping.json"
    sidecar.write_text('{"vuid": "WRONG", "cd": null}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "voterfile",
            "match",
            str(roster),
            str(vf_copy),
            "--no-interactive",
            "--redetect",
            "--output-dir",
            str(tmp_path),
            "--no-save-mapping",
        ],
    )
    assert result.exit_code == 0, result.stdout
    report = json.loads((tmp_path / "match_report_roster.json").read_text(encoding="utf-8"))
    assert report["matched_count"] == 1
    # Sidecar not rewritten when --no-save-mapping
    assert json.loads(sidecar.read_text(encoding="utf-8"))["vuid"] == "WRONG"


def test_voterfile_match_failure_does_not_save_sidecar(tmp_path: Path, monkeypatch) -> None:
    roster = tmp_path / "roster.csv"
    _write_minimal_roster(roster)
    vf_copy = tmp_path / "voterfile.csv"
    vf_copy.write_text(FIXTURE_VOTERFILE.read_text(encoding="utf-8"), encoding="utf-8")
    sidecar = tmp_path / "voterfile.mapping.json"

    def _boom(*_args, **_kwargs):
        raise OSError("simulated DuckDB failure")

    monkeypatch.setattr(
        "texas_turnout_scraper.voterfile.match_voterfile_to_roster",
        _boom,
    )
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
    assert result.exit_code == 1
    assert not sidecar.exists()


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
    assert "Column mapping saved" in result.stdout


def test_voterfile_match_overwrites_sidecar_after_success(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    _write_minimal_roster(roster)
    vf_copy = tmp_path / "voterfile.csv"
    vf_copy.write_text(FIXTURE_VOTERFILE.read_text(encoding="utf-8"), encoding="utf-8")
    sidecar = tmp_path / "voterfile.mapping.json"
    sidecar.write_text('{"vuid": "WRONG"}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "voterfile",
            "match",
            str(roster),
            str(vf_copy),
            "--no-interactive",
            "--redetect",
            "--output-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["vuid"] == "VUID"


def test_voterfile_match_includes_gap_report_for_civix_roster(tmp_path: Path) -> None:
    election_dir = tmp_path / "data" / "elections" / "civix" / "58315"
    election_dir.mkdir(parents=True)
    roster = election_dir / "roster_ev_58315.csv"
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
        roster,
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

    election = CivixElection(
        source_election_id="58315",
        id=58315,
        type="EV",
        election_date=date(2026, 5, 26),
        election_name="2026 REPUBLICAN PRIMARY RUNOFF ELECTION",
        certified=False,
        early_voting_dates=[CivixElectionDate(date=date(2026, 5, 22), date_turnout_id=1)],
        counties=[],
    )

    with patch("texas_turnout_scraper.civix.CivixClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.list_elections.return_value = [election]

        result = runner.invoke(
            app,
            [
                "voterfile",
                "match",
                str(roster),
                str(FIXTURE_VOTERFILE),
                "--no-interactive",
                "--output-dir",
                str(election_dir),
                "--no-save-mapping",
                "--gap-turnout-source",
                "stored",
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert "Turnout vs Roster Gap" in result.stdout
    gap_json = election_dir / "gap_report_roster_ev_58315.json"
    assert gap_json.exists()
    match_report = json.loads((election_dir / "match_report_roster_ev_58315.json").read_text())
    assert match_report["turnout_roster_gap"] is not None
    assert match_report["turnout_roster_gap"]["gap_total"] == 124

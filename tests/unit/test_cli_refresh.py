"""Unit tests for refresh-all helpers and index orchestration."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from texas_turnout_scraper.cli import (
    _build_civix_index_entries,
    _legacy_election_date,
    _load_index_sections,
    _parse_ev_date,
    _roster_is_fresh,
    _update_election_index,
    _write_election_index,
    app,
)
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import CivixElection, LegacyElection
from texas_turnout_scraper.writer import (
    stored_audit_ev_path,
    stored_roster_ev_path,
    write_roster_csv,
)

runner = CliRunner()


def _minimal_roster(path: Path, election_id: str = "53813") -> None:
    from texas_turnout_scraper.models import VoterRecord

    path.parent.mkdir(parents=True, exist_ok=True)
    write_roster_csv(
        [
            VoterRecord(
                id_voter="0123456789",
                voting_method=VoteMethod.IN_PERSON,
                precinct="100",
                county="HARRIS",
                election_id=election_id,
                report_date=date(2026, 2, 17),
            )
        ],
        path,
    )


def test_roster_is_fresh_uses_index_last_refreshed_over_checkout_mtime(
    tmp_path: Path,
) -> None:
    roster_path = tmp_path / "civix" / "53813" / "roster_ev_53813.csv"
    _minimal_roster(roster_path)

    stale_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "last_updated": stale_time,
                "civix": {
                    "elections": [
                        {
                            "source_election_id": "53813",
                            "last_refreshed": stale_time,
                        }
                    ]
                },
                "legacy": {"elections": []},
            }
        )
    )

    assert not _roster_is_fresh(
        roster_path,
        index_path=index_path,
        source_prefix="civix",
        election_id="53813",
        max_age_hours=24,
    )

    fresh_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_path.write_text(
        json.dumps(
            {
                "last_updated": fresh_time,
                "civix": {
                    "elections": [
                        {
                            "source_election_id": "53813",
                            "last_refreshed": fresh_time,
                        }
                    ]
                },
                "legacy": {"elections": []},
            }
        )
    )

    assert _roster_is_fresh(
        roster_path,
        index_path=index_path,
        source_prefix="civix",
        election_id="53813",
        max_age_hours=24,
    )


def test_roster_is_fresh_max_age_zero_always_stale(tmp_path: Path) -> None:
    roster_path = tmp_path / "roster_ev_53813.csv"
    _minimal_roster(roster_path)

    assert not _roster_is_fresh(roster_path, max_age_hours=0)


def test_write_election_index_skips_unchanged_payload(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    entries = [{"source_election_id": "53813", "total_records": 1}]

    assert _write_election_index(
        index_path,
        civix_entries=entries,
        legacy_entries=[],
    )
    first = index_path.read_text()

    assert not _write_election_index(
        index_path,
        civix_entries=entries,
        legacy_entries=[],
    )
    assert index_path.read_text() == first


def test_update_election_index_sets_refreshed_timestamp(tmp_path: Path) -> None:
    output_dir = tmp_path / "civix"
    roster_path = output_dir / "53813" / "roster_ev_53813.csv"
    _minimal_roster(roster_path)
    index_path = tmp_path / "index.json"

    elections = [
        CivixElection(
            source_election_id="53813",
            id=53813,
            type="EV",
            election_date=date(2026, 3, 3),
            election_name="2026 REPUBLICAN PRIMARY ELECTION",
            certified=True,
            early_voting_dates=[],
            counties=[],
        )
    ]

    assert _update_election_index(
        index_path,
        civix_output_dir=output_dir,
        civix_elections=elections,
        refreshed_civix_ids={"53813"},
    )

    data = json.loads(index_path.read_text())
    entry = data["civix"]["elections"][0]
    assert entry["source_election_id"] == "53813"
    assert entry["total_records"] == 1
    assert "last_refreshed" in entry


def test_build_civix_index_entries_preserves_existing_last_refreshed(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "civix"
    roster_path = output_dir / "53813" / "roster_ev_53813.csv"
    _minimal_roster(roster_path)
    index_path = tmp_path / "index.json"
    preserved = "2026-01-01T00:00:00Z"
    index_path.write_text(
        json.dumps(
            {
                "last_updated": preserved,
                "civix": {
                    "elections": [
                        {
                            "source_election_id": "53813",
                            "last_refreshed": preserved,
                        }
                    ]
                },
                "legacy": {"elections": []},
            }
        )
    )

    elections = [
        CivixElection(
            source_election_id="53813",
            id=53813,
            type="EV",
            election_date=date(2026, 3, 3),
            election_name="2026 REPUBLICAN PRIMARY ELECTION",
            certified=True,
            early_voting_dates=[],
            counties=[],
        )
    ]
    entries = _build_civix_index_entries(
        output_dir,
        elections,
        index_path=index_path,
        refreshed_ids=set(),
    )
    assert entries[0]["last_refreshed"] == preserved


def test_legacy_election_date_uses_election_year_when_ev_dates_missing() -> None:
    election = LegacyElection(
        source_election_id="49664",
        election_name="2024 GENERAL ELECTION",
        election_year=2024,
        ev_dates=[],
    )
    assert _legacy_election_date(election) == date(2024, 11, 1)


def test_load_index_sections_returns_empty_when_missing(tmp_path: Path) -> None:
    civix_entries, legacy_entries = _load_index_sections(tmp_path / "missing.json")
    assert civix_entries == []
    assert legacy_entries == []


def test_parse_ev_date_accepts_iso_format() -> None:
    assert _parse_ev_date("2024-10-21") == date(2024, 10, 21)


def test_parse_ev_date_rejects_invalid_format() -> None:
    with pytest.raises(typer.BadParameter):
        _parse_ev_date("10/21/2024")


def test_stored_roster_ev_path_layout() -> None:
    path = stored_roster_ev_path(Path("data"), "civix", "53813")
    assert path == Path("data/elections/civix/53813/roster_ev_53813.csv")


def test_stored_audit_ev_path_layout() -> None:
    path = stored_audit_ev_path(Path("data"), "legacy", "49664")
    assert path == Path("data/elections/legacy/49664/audit_ev_49664.json")


def test_tx_turnout_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "civix" in result.stdout
    assert "legacy" in result.stdout
    assert "audit" in result.stdout
    assert "voterfile" in result.stdout

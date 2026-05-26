"""Unit tests for mcp_server tools — HTTP mocked via respx or legacy_api patches."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import respx

from texas_turnout_scraper.civix import API_PREFIX, BASE_URL
from texas_turnout_scraper.enums import ElectionType, VoteMethod
from texas_turnout_scraper.mcp_server import (
    civix_fetch_county_roster,
    civix_fetch_ed_turnout,
    civix_fetch_polling_places,
    civix_fetch_turnout,
    civix_list_elections,
    legacy_fetch_county_roster,
    legacy_fetch_turnout,
    legacy_list_elections,
    run_audit,
)
from texas_turnout_scraper.models import (
    AuditReport,
    CountyRoster,
    CountyTurnout,
    LegacyElection,
    VoterRecord,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "early_voting"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _civix_json_response(data: dict) -> httpx.Response:
    import base64

    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return httpx.Response(200, json={"upload": encoded})


def _civix_csv_response(csv_text: str) -> httpx.Response:
    import base64

    encoded = base64.b64encode(csv_text.encode()).decode()
    return httpx.Response(200, json={"upload": encoded})


@respx.mock
def test_civix_list_elections_tool() -> None:
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={"type": "EVR_ELECTION"},
    ).mock(return_value=_civix_json_response(_load_json("civix_election_index.json")))
    result = civix_list_elections()
    assert isinstance(result, list)
    assert result
    assert "source_election_id" in result[0]
    assert "election_type" in result[0]


@respx.mock
def test_civix_fetch_turnout_tool() -> None:
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={
            "type": "EVR_EARLYVOTING",
            "electionId": "53813",
            "electionDate": "02/27/2026",
        },
    ).mock(return_value=_civix_json_response(_load_json("civix_earlyvoting_53813.json")))
    rows = civix_fetch_turnout(election_id=53813, election_date="2026-02-27")
    assert isinstance(rows, list)
    assert rows[0]["county"]
    assert rows[0]["source"] == "civix"


@respx.mock
def test_civix_fetch_county_roster_tool() -> None:
    csv_text = _load_text("civix_roster_harris_sample.csv")
    respx.get(f"{BASE_URL}{API_PREFIX}/getFileByFormat").mock(
        return_value=_civix_csv_response(csv_text)
    )
    summary = civix_fetch_county_roster(
        election_id=53813,
        election_date="2026-02-27",
        county_name="HARRIS",
        county_id=101,
    )
    assert summary["county"] == "HARRIS"
    assert summary["total_voters"] >= 0
    assert "in_person" in summary
    assert "mail_in" in summary
    assert "id_voter" not in summary


@respx.mock
def test_civix_fetch_ed_turnout_tool() -> None:
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={
            "type": "EVR_ELECTIONDAYTURNOUT",
            "electionId": "53813",
            "electionDate": "03/03/2026",
        },
    ).mock(return_value=_civix_json_response(_load_json("civix_ed_turnout_53813.json")))
    rows = civix_fetch_ed_turnout(election_id=53813, election_date="2026-03-03")
    assert isinstance(rows, list)
    assert rows[0]["source"] == "civix"


@respx.mock
def test_civix_fetch_polling_places_tool() -> None:
    csv_text = "COUNTY,POLLING_PLACE\nHARRIS,Example Location\n"
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFileByFormat",
        params={
            "type": "EVR_COUNTYPLACEINFO",
            "electionId": "53813",
            "name": "STATEWIDE_POLLING_PLACE_INFO",
            "format": "csv",
        },
    ).mock(return_value=_civix_csv_response(csv_text))
    text = civix_fetch_polling_places(election_id=53813)
    assert "HARRIS" in text


@patch("texas_turnout_scraper.legacy_api.list_elections")
def test_legacy_list_elections_tool(mock_list: MagicMock) -> None:
    mock_list.return_value = [
        LegacyElection(
            source_election_id="49664",
            election_name="2024 GENERAL",
            election_type=ElectionType.GENERAL,
            election_year=2024,
        )
    ]
    rows = legacy_list_elections()
    assert rows[0]["source_election_id"] == "49664"


@patch("texas_turnout_scraper.legacy_api.fetch_county_turnout")
def test_legacy_fetch_turnout_tool(mock_turnout: MagicMock) -> None:
    mock_turnout.return_value = [
        CountyTurnout(
            election_id="49664",
            report_date=date(2024, 10, 21),
            county="LOVING",
            registered_voters=100,
            in_person_votes_on_date=10,
            total_in_person_votes=10,
            total_mail_votes=5,
            roster_available=True,
            source="legacy",
        )
    ]
    rows = legacy_fetch_turnout(source_election_id="49664", ev_date="2024-10-21")
    assert rows[0]["county"] == "LOVING"


@patch("texas_turnout_scraper.legacy_api.fetch_single_county_roster")
def test_legacy_fetch_county_roster_tool(mock_roster: MagicMock) -> None:
    mock_roster.return_value = CountyRoster(
        county="LOVING",
        election_id="49664",
        report_date=date(2024, 10, 21),
        source="legacy",
        records=[
            VoterRecord(
                id_voter="0123456789",
                voting_method=VoteMethod.IN_PERSON,
                precinct="1",
                county="LOVING",
                election_id="49664",
                report_date=date(2024, 10, 21),
            )
        ],
    )
    summary = legacy_fetch_county_roster(
        source_election_id="49664",
        ev_date="2024-10-21",
        county_id="149",
    )
    assert summary["total_voters"] == 1
    assert summary["in_person"] == 1


@patch("texas_turnout_scraper.writer.read_roster_csv")
@patch("texas_turnout_scraper.audit.audit_records")
@patch("texas_turnout_scraper.writer.stored_roster_ev_path")
def test_run_audit_tool(
    mock_path_fn: MagicMock,
    mock_audit: MagicMock,
    mock_read: MagicMock,
    tmp_path: Path,
) -> None:
    roster_file = tmp_path / "roster_ev_test.csv"
    roster_file.write_text("header\n", encoding="utf-8")
    mock_path_fn.return_value = roster_file
    mock_read.return_value = []
    mock_audit.return_value = AuditReport(
        election_id="test",
        report_date=date(2024, 10, 21),
        source="civix",
        total_records=0,
        unique_vuids=0,
        duplicate_vuid_count=0,
        cross_method_duplicate_count=0,
    )
    report = run_audit(election_id="test", ev_date="2024-10-21", source="civix")
    assert report["election_id"] == "test"
    mock_audit.assert_called_once()

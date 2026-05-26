"""Unit tests for civix.py — all HTTP mocked via respx."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from texas_turnout_scraper.civix import (
    API_PREFIX,
    BASE_URL,
    CivixClient,
    _decode_envelope,
    fetch_county_roster,
)
from texas_turnout_scraper.enums import ElectionType, VoteMethod
from texas_turnout_scraper.http_transport import _MAX_HTTP_RETRIES

FIXTURES = Path(__file__).parent.parent / "fixtures" / "early_voting"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _civix_json_response(data: dict) -> httpx.Response:
    """Wrap a dict in the Civix base64 envelope."""
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return httpx.Response(200, json={"upload": encoded})


def _civix_csv_response(csv_text: str) -> httpx.Response:
    """Wrap CSV text in the Civix base64 envelope."""
    encoded = base64.b64encode(csv_text.encode()).decode()
    return httpx.Response(200, json={"upload": encoded})


def _civix_bytes_response(payload: bytes) -> httpx.Response:
    """Wrap raw bytes in the Civix base64 envelope."""
    encoded = base64.b64encode(payload).decode()
    return httpx.Response(200, json={"upload": encoded})


def _civix_zip_response(zip_bytes: bytes) -> httpx.Response:
    """Wrap ZIP bytes in the Civix base64 envelope."""
    return _civix_bytes_response(zip_bytes)


def _assert_http_mocked(expected_calls: int = 1) -> None:
    """Fail if any request escaped respx or call count is wrong."""
    assert len(respx.calls) == expected_calls, (
        f"Expected {expected_calls} mocked HTTP call(s), got {len(respx.calls)}"
    )


def _mock_ev_roster_csv() -> None:
    csv_text = _load_text("civix_roster_harris_sample.csv")
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFileByFormat",
        params={
            "type": "EVR_EARLYVOTING",
            "electionId": "53813",
            "electionDate": "02/27/2026",
            "county": "HARRIS",
            "countyId": "101",
            "format": "csv",
        },
    ).mock(return_value=_civix_csv_response(csv_text))


def _mock_ed_roster_zip(*, row_count_per_csv: int = 2) -> bytes:
    """Build a multi-file ZIP mock payload and register the respx route."""
    csv_a = (
        '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n'
        + "\n".join(
            f'"DOE, VOTER {i}","000000000{i}","IN-PERSON","510"'
            for i in range(1, row_count_per_csv + 1)
        )
        + "\n"
    )
    csv_b = (
        '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n'
        '"DOE, EXTRA","0000000099","MAIL-IN","512"\n'
    )
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("harris_part_a.csv", csv_a)
        zf.writestr("harris_part_b.csv", csv_b)
        zf.writestr("readme.txt", "not a roster")
    zip_bytes = zip_buffer.getvalue()

    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFileByFormat",
        params={
            "type": "EVR_ELECTIONDAYTURNOUT",
            "electionId": "53813",
            "electionDate": "03/03/2026",
            "county": "HARRIS",
            "countyId": "101",
            "format": "zip",
        },
    ).mock(return_value=_civix_zip_response(zip_bytes))
    return zip_bytes


@respx.mock
def test_list_elections_parses_correctly() -> None:
    election_data = _load_json("civix_election_index.json")
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={"type": "EVR_ELECTION"},
    ).mock(return_value=_civix_json_response(election_data))

    with CivixClient(http_backend="httpx") as client:
        elections = client.list_elections()

    assert len(elections) == 2
    first = elections[0]
    assert first.source_election_id == "53813"
    assert isinstance(first.source_election_id, str)
    assert first.election_type == ElectionType.PRIMARY
    assert first.certified is True
    assert first.election_date == date(2026, 3, 3)
    assert len(first.early_voting_dates) == 3
    assert first.early_voting_dates[0].date == date(2026, 2, 17)
    assert len(first.counties) == 3
    assert first.counties[0].name == "ANDERSON"
    assert first.counties[0].county_id == 1

    second = elections[1]
    assert second.election_type == ElectionType.SPECIAL
    assert isinstance(second.source_election_id, str)

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ev_turnout_parses_correctly() -> None:
    turnout_data = _load_json("civix_earlyvoting_53813.json")
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={
            "type": "EVR_EARLYVOTING",
            "electionId": "53813",
            "electionDate": "02/27/2026",
        },
    ).mock(return_value=_civix_json_response(turnout_data))

    report_date = date(2026, 2, 27)
    with CivixClient(http_backend="httpx") as client:
        results = client.fetch_ev_turnout(53813, report_date)

    assert len(results) == 3

    anderson = next(r for r in results if r.county == "ANDERSON")
    assert anderson.county_id == 1
    assert anderson.registered_voters == 30678
    assert anderson.in_person_votes_on_date == 707
    assert anderson.total_in_person_votes == 3963
    assert anderson.total_mail_votes == 0
    assert anderson.roster_available is True
    assert anderson.report_date == report_date

    harris = next(r for r in results if r.county == "HARRIS")
    assert harris.county_id == 101
    assert harris.total_in_person_votes == 88500
    assert harris.total_mail_votes == 1200
    assert harris.roster_available is True

    travis = next(r for r in results if r.county == "TRAVIS")
    assert travis.county_id == 227
    assert travis.roster_available is False

    for county_turnout in results:
        assert county_turnout.election_id == "53813"
        assert county_turnout.report_date == report_date

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ev_turnout_roster_available_logic() -> None:
    turnout_data = {
        "turnout_by_county": [
            {
                "name": "STRING_COUNTY",
                "id": 1,
                "registered_voters": 100,
                "in_person_votes_on_date": 10,
                "total_in_person_votes_for_election": 50,
                "total_mail_votes_for_election": 5,
                "voter_details_report": "path/to/report",
            },
            {
                "name": "TRUE_COUNTY",
                "id": 2,
                "registered_voters": 200,
                "in_person_votes_on_date": 20,
                "total_in_person_votes_for_election": 60,
                "total_mail_votes_for_election": 6,
                "voter_details_report": True,
            },
            {
                "name": "FALSE_COUNTY",
                "id": 3,
                "registered_voters": 300,
                "in_person_votes_on_date": 30,
                "total_in_person_votes_for_election": 70,
                "total_mail_votes_for_election": 7,
                "voter_details_report": False,
            },
            {
                "name": "MISSING_COUNTY",
                "id": 4,
                "registered_voters": 400,
                "in_person_votes_on_date": 40,
                "total_in_person_votes_for_election": 80,
                "total_mail_votes_for_election": 8,
            },
        ]
    }
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={
            "type": "EVR_EARLYVOTING",
            "electionId": "53813",
            "electionDate": "02/27/2026",
        },
    ).mock(return_value=_civix_json_response(turnout_data))

    with CivixClient(http_backend="httpx") as client:
        results = client.fetch_ev_turnout(53813, date(2026, 2, 27))

    by_county = {r.county: r.roster_available for r in results}
    assert by_county["STRING_COUNTY"] is True
    assert by_county["TRUE_COUNTY"] is True
    assert by_county["FALSE_COUNTY"] is False
    assert by_county["MISSING_COUNTY"] is False

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ev_roster_csv_returns_voter_records() -> None:
    _mock_ev_roster_csv()

    with CivixClient(http_backend="httpx") as client:
        records = client.fetch_ev_roster_csv(53813, date(2026, 2, 27), "HARRIS", 101)

    assert len(records) == 10
    assert all(isinstance(r.id_voter, str) for r in records)
    assert records[0].voting_method == VoteMethod.IN_PERSON
    assert records[2].voting_method == VoteMethod.MAIL_IN
    for record in records:
        assert record.county == "HARRIS"
        assert record.election_id == "53813"
        assert record.report_date == date(2026, 2, 27)

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ev_roster_csv_id_voter_is_always_string() -> None:
    _mock_ev_roster_csv()

    with CivixClient(http_backend="httpx") as client:
        records = client.fetch_ev_roster_csv(53813, date(2026, 2, 27), "HARRIS", 101)

    for record in records:
        assert isinstance(record.id_voter, str)
    assert records[0].id_voter == "0000000001"
    assert records[0].id_voter != "1"

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ev_roster_csv_normalizes_unpadded_vuid() -> None:
    csv_text = (
        '"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n'
        '"DOE, JOHN A","123456789","IN-PERSON","510"\n'
    )
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFileByFormat",
        params={
            "type": "EVR_EARLYVOTING",
            "electionId": "53813",
            "electionDate": "02/27/2026",
            "county": "HARRIS",
            "countyId": "101",
            "format": "csv",
        },
    ).mock(return_value=_civix_csv_response(csv_text))

    with CivixClient(http_backend="httpx") as client:
        records = client.fetch_ev_roster_csv(53813, date(2026, 2, 27), "HARRIS", 101)

    assert len(records) == 1
    assert records[0].id_voter == "0123456789"

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ev_roster_csv_voter_name_stored() -> None:
    _mock_ev_roster_csv()

    with CivixClient(http_backend="httpx") as client:
        records = client.fetch_ev_roster_csv(53813, date(2026, 2, 27), "HARRIS", 101)

    assert records[0].voter_name == "DOE, JOHN A"

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ed_turnout_voter_details_report_true_is_roster_available() -> None:
    turnout_data = _load_json("civix_ed_turnout_53813.json")
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={
            "type": "EVR_ELECTIONDAYTURNOUT",
            "electionId": "53813",
            "electionDate": "03/03/2026",
        },
    ).mock(return_value=_civix_json_response(turnout_data))

    with CivixClient(http_backend="httpx") as client:
        results = client.fetch_ed_turnout(53813, date(2026, 3, 3))

    assert len(results) == 2
    assert all(r.roster_available is True for r in results)

    _assert_http_mocked(1)


@respx.mock
def test_fetch_ed_roster_zip_parses_all_csvs_in_zip() -> None:
    _mock_ed_roster_zip(row_count_per_csv=2)

    with CivixClient(http_backend="httpx") as client:
        records = client.fetch_ed_roster_zip(53813, date(2026, 3, 3), "HARRIS", 101)

    # Two rows from part_a + one row from part_b; readme.txt skipped
    assert len(records) == 3
    for record in records:
        assert record.county == "HARRIS"
        assert record.election_id == "53813"
        assert record.report_date == date(2026, 3, 3)
        assert len(record.id_voter) == 10

    _assert_http_mocked(1)


@respx.mock
def test_fetch_county_roster_convenience_function_ev_path() -> None:
    _mock_ev_roster_csv()

    with CivixClient(http_backend="httpx") as client:
        roster = fetch_county_roster(
            client,
            election_id=53813,
            election_date=date(2026, 2, 27),
            county_name="HARRIS",
            county_id=101,
            is_election_day=False,
        )

    assert roster.county == "HARRIS"
    assert roster.county_id == 101
    assert roster.election_id == "53813"
    assert roster.report_date == date(2026, 2, 27)
    assert roster.source == "civix"
    assert roster.total_voters == len(roster.records)
    assert roster.total_voters == 10
    for record in roster.records:
        assert record.county == "HARRIS"
        assert record.election_id == "53813"

    _assert_http_mocked(1)


@respx.mock
def test_fetch_county_roster_convenience_function_election_day_path() -> None:
    _mock_ed_roster_zip(row_count_per_csv=2)

    with CivixClient(http_backend="httpx") as client:
        roster = fetch_county_roster(
            client,
            election_id=53813,
            election_date=date(2026, 3, 3),
            county_name="HARRIS",
            county_id=101,
            is_election_day=True,
        )

    assert roster.county == "HARRIS"
    assert roster.county_id == 101
    assert roster.election_id == "53813"
    assert roster.report_date == date(2026, 3, 3)
    assert roster.source == "civix"
    assert roster.total_voters == 3
    for record in roster.records:
        assert record.county == "HARRIS"
        assert record.report_date == date(2026, 3, 3)

    _assert_http_mocked(1)


@respx.mock
def test_fetch_statewide_returns_decoded_bytes() -> None:
    payload = b"col1,col2\n1,2\n"
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={
            "type": "EVR_STATEWIDE",
            "electionId": "53813",
            "electionDate": "02/27/2026",
        },
    ).mock(return_value=_civix_bytes_response(payload))

    with CivixClient(http_backend="httpx") as client:
        result = client.fetch_statewide(53813, date(2026, 2, 27))

    assert result == payload

    _assert_http_mocked(1)


@respx.mock
def test_fetch_polling_places_returns_csv_text() -> None:
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

    with CivixClient(http_backend="httpx") as client:
        result = client.fetch_polling_places(53813)

    assert result == csv_text
    assert "HARRIS" in result

    _assert_http_mocked(1)


@respx.mock
def test_decode_envelope_missing_upload_raises() -> None:
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={"type": "EVR_ELECTION"},
    ).mock(return_value=httpx.Response(200, json={}))

    with CivixClient(http_backend="httpx") as client:
        with pytest.raises(KeyError):
            client.list_elections()

    _assert_http_mocked(1)


@respx.mock
def test_decode_envelope_empty_body_returns_empty_bytes() -> None:
    response = httpx.Response(200, text="")
    assert _decode_envelope(response) == b""


@respx.mock
def test_fetch_ev_roster_csv_empty_body_returns_no_records() -> None:
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFileByFormat",
        params={
            "type": "EVR_EARLYVOTING",
            "electionId": "53813",
            "electionDate": "02/27/2026",
            "county": "LOVING",
            "countyId": "151",
            "format": "csv",
        },
    ).mock(return_value=httpx.Response(200, text=""))

    with CivixClient(http_backend="httpx") as client:
        records = client.fetch_ev_roster_csv(
            53813,
            date(2026, 2, 27),
            "LOVING",
            151,
        )

    assert records == []
    _assert_http_mocked(1)


@patch("texas_turnout_scraper.civix.time.sleep")
@respx.mock
def test_pacing_enforced(mock_sleep) -> None:
    election_data = _load_json("civix_election_index.json")
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={"type": "EVR_ELECTION"},
    ).mock(return_value=_civix_json_response(election_data))

    with CivixClient(http_backend="httpx", pace_seconds=1.0) as client:
        client._last_request = 1.0
        with patch("texas_turnout_scraper.civix.time.monotonic", return_value=1.05):
            client.list_elections()

    mock_sleep.assert_called_once()
    assert mock_sleep.call_args.args[0] == pytest.approx(0.95, abs=0.001)

    _assert_http_mocked(1)


@respx.mock
def test_http_error_raises() -> None:
    respx.get(
        f"{BASE_URL}{API_PREFIX}/getFile",
        params={"type": "EVR_ELECTION"},
    ).mock(return_value=httpx.Response(502))

    with CivixClient(http_backend="httpx") as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.list_elections()

    _assert_http_mocked(_MAX_HTTP_RETRIES + 1)

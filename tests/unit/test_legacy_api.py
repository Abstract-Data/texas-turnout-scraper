"""Unit tests for legacy_api session-managed facades."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from texas_turnout_scraper import legacy_api
from texas_turnout_scraper.session import LegacySession
from texas_turnout_scraper.writer import read_roster_csv

FIXTURES = Path(__file__).parent.parent / "fixtures" / "early_voting"
BASE_URL = LegacySession.BASE_URL
_ELECTION_ID = "49664"
_EV_DATE = date(2024, 10, 21)
_LOVING_CSV = (FIXTURES / "legacy_voter_info_loving.csv").read_text()


def _synthetic_county_csv(county_label: str, count: int) -> str:
    lines = ['"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"']
    for i in range(1, count + 1):
        lines.append(f'"DOE, {county_label} {i}","300000000{i:02d}","IN-PERSON","1"')
    return "\n".join(lines) + "\n"


_COUNTY_CSV_BY_ID = {
    "149": _LOVING_CSV,
    "101": _synthetic_county_csv("HARRIS", 10),
    "227": _synthetic_county_csv("TRAVIS", 10),
}


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _mock_establish_and_ev_dates() -> None:
    respx.get(f"{BASE_URL}/Elections/getElectionDetails.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_election_index.html"))
    )
    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))
    )


@respx.mock
def test_list_elections_facade() -> None:
    _mock_establish_and_ev_dates()

    elections = legacy_api.list_elections(http_backend="httpx", pace_seconds=0.0)

    assert len(elections) == 3
    assert all(isinstance(e.source_election_id, str) for e in elections)


@respx.mock
def test_fetch_county_turnout_facade() -> None:
    _mock_establish_and_ev_dates()
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_details_49664.html"))
    )

    rows = legacy_api.fetch_county_turnout(
        _ELECTION_ID,
        _EV_DATE,
        http_backend="httpx",
        pace_seconds=0.0,
    )

    assert len(rows) == 3
    assert {r.county for r in rows} == {"LOVING", "HARRIS", "TRAVIS"}


@respx.mock
def test_fetch_single_county_roster_facade() -> None:
    _mock_establish_and_ev_dates()
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        return_value=httpx.Response(200, text=_LOVING_CSV)
    )

    roster = legacy_api.fetch_single_county_roster(
        _ELECTION_ID,
        _EV_DATE,
        county_id="149",
        county_name="LOVING",
        http_backend="httpx",
        pace_seconds=0.0,
    )

    assert roster.county == "LOVING"
    assert len(roster.records) == 6
    assert all(isinstance(r.id_voter, str) for r in roster.records)


@respx.mock
def test_fetch_roster_resolves_county_ids_from_turnout_html() -> None:
    _mock_establish_and_ev_dates()
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_details_49664.html"))
    )

    requested_town_ids: list[str] = []

    def roster_handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        for county_id in _COUNTY_CSV_BY_ID:
            if f"idTown={county_id}" in body:
                requested_town_ids.append(county_id)
                return httpx.Response(200, text=_COUNTY_CSV_BY_ID[county_id])
        return httpx.Response(200, text="")

    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(side_effect=roster_handler)

    rosters = legacy_api.fetch_roster(
        _ELECTION_ID,
        _EV_DATE,
        http_backend="httpx",
        pace_seconds=0.0,
    )

    assert requested_town_ids == ["149", "101", "227"]
    assert len(rosters) == 3
    counties = {r.county for r in rosters}
    assert counties == {"LOVING", "HARRIS", "TRAVIS"}
    record_counts = {r.county: len(r.records) for r in rosters}
    assert record_counts == {"LOVING": 6, "HARRIS": 10, "TRAVIS": 10}


@respx.mock
def test_fetch_roster_raises_when_turnout_html_has_no_counties() -> None:
    """ValueError from facade; CLI legacy roster maps this to stderr + exit 1 (test_cli_legacy)."""
    _mock_establish_and_ev_dates()
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(
            200,
            text="<html><body><table><tr><td>STATEWIDE</td></tr></table></body></html>",
        )
    )
    roster_route = respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        return_value=httpx.Response(200, text=_LOVING_CSV)
    )

    with pytest.raises(ValueError, match="No county IDs found"):
        legacy_api.fetch_roster(
            _ELECTION_ID,
            _EV_DATE,
            http_backend="httpx",
            pace_seconds=0.0,
        )

    assert roster_route.call_count == 0


@respx.mock
def test_fetch_roster_strategy_b_facade_writes_zip(tmp_path: Path) -> None:
    _mock_establish_and_ev_dates()
    zip_bytes = b"PK\x03\x04strategy-b"
    respx.post(f"{BASE_URL}/Elections/downloadParticipationCountReport.do").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    rosters = legacy_api.fetch_roster(
        _ELECTION_ID,
        _EV_DATE,
        strategy="B",
        out_dir=tmp_path,
        http_backend="httpx",
        pace_seconds=0.0,
    )

    assert rosters == []
    zip_path = tmp_path / "roster_2024-10-21_bulk.zip"
    assert zip_path.read_bytes() == zip_bytes


@respx.mock
def test_fetch_roster_strategy_a_writes_per_county_csvs(tmp_path: Path) -> None:
    _mock_establish_and_ev_dates()
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_details_49664.html"))
    )

    def roster_handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        for county_id in _COUNTY_CSV_BY_ID:
            if f"idTown={county_id}" in body:
                return httpx.Response(200, text=_COUNTY_CSV_BY_ID[county_id])
        return httpx.Response(200, text="")

    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(side_effect=roster_handler)

    rosters = legacy_api.fetch_roster(
        _ELECTION_ID,
        _EV_DATE,
        out_dir=tmp_path,
        http_backend="httpx",
        pace_seconds=0.0,
    )

    assert len(rosters) == 3
    record_counts = {r.county: len(r.records) for r in rosters}
    assert record_counts == {"LOVING": 6, "HARRIS": 10, "TRAVIS": 10}
    for roster in rosters:
        csv_path = tmp_path / f"roster_{_EV_DATE}_{roster.county}.csv"
        assert csv_path.exists()
        records = read_roster_csv(csv_path)
        assert len(records) == roster.total_voters
        assert records[0].county == roster.county


@respx.mock
def test_fetch_roster_auto_county_ids_primes_once() -> None:
    _mock_establish_and_ev_dates()
    prime_calls = 0

    def _count_prime(request: httpx.Request) -> httpx.Response:
        nonlocal prime_calls
        prime_calls += 1
        return httpx.Response(200, text=_load_text("legacy_ev_dates_49664.html"))

    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(side_effect=_count_prime)
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(
        return_value=httpx.Response(200, text=_load_text("legacy_ev_details_49664.html"))
    )
    respx.post(f"{BASE_URL}/Elections/downloadVoterInfoReport.do").mock(
        return_value=httpx.Response(200, text=_LOVING_CSV)
    )

    rosters = legacy_api.fetch_roster(
        _ELECTION_ID,
        _EV_DATE,
        http_backend="httpx",
        pace_seconds=0.0,
    )

    assert len(rosters) == 3
    assert prime_calls == 1

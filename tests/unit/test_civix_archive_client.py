"""Unit tests for the Civix archive client. Ported from Quick Results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from texas_turnout_scraper.civix.archive import (
    CivixArchiveClient,
    CivixArchiveNotFoundError,
    discover_archive_election_refs,
    parse_constants_version,
    parse_election_version,
)
from texas_turnout_scraper.civix.models import CivixElectionRef

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "civix_archive"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("texas_turnout_scraper.http_transport._json_retry_sleep", _sleep)


def test_parse_constants_version_reads_version_no() -> None:
    payload = json.loads((FIXTURES_DIR / "version_root.json").read_text(encoding="utf-8"))
    assert parse_constants_version(payload) == 404


def test_parse_election_version_reads_elec_date() -> None:
    payload = json.loads((FIXTURES_DIR / "election_49664_version.json").read_text(encoding="utf-8"))
    version = parse_election_version(49664, payload)
    assert version.version_no == 1012
    assert version.elec_date == "11052024"
    assert version.election_id == "49664"


def test_discover_archive_election_refs_filters_year_range() -> None:
    constants = json.loads(
        (FIXTURES_DIR / "election_constants_mini.json").read_text(encoding="utf-8")
    )
    refs = discover_archive_election_refs(constants, min_year="2024", max_year="2024")
    assert {ref.election_id for ref in refs} == {"49664", "49665", "50676"}


@pytest.mark.asyncio
async def test_fetch_office_summary_raises_not_found() -> None:
    archive_client = CivixArchiveClient(pacing_seconds=0)

    async def fake_get_json(path: str) -> dict:
        raise CivixArchiveNotFoundError(path)

    archive_client._get_json = fake_get_json  # ty: ignore[invalid-assignment] # reason: test double
    with pytest.raises(CivixArchiveNotFoundError):
        await archive_client.fetch_office_summary(99999, 1)


@pytest.mark.asyncio
async def test_fetch_election_bundle_tolerates_missing_county_detail() -> None:
    archive_client = CivixArchiveClient(pacing_seconds=0)
    election_ref = CivixElectionRef(
        election_id="49664",
        name="2024 NOVEMBER 5TH GENERAL ELECTION",
        year="2024",
        election_type_code="G",
        election_class_code="SW",
    )

    async def fake_fetch_version(election_id: str) -> object:
        assert election_id == "49664"
        return parse_election_version(49664, {"___versionNo": 1012, "elecDate": "11052024"})

    async def fake_fetch_office_summary(election_id: str, version_no: int) -> dict:
        assert election_id == "49664" and version_no == 1012
        return {"OS": []}

    async def fake_fetch_county_detail(election_id: str, version_no: int) -> dict:
        raise CivixArchiveNotFoundError(
            f"/static/data/election/{election_id}/{version_no}/County.json"
        )

    archive_client.fetch_election_version = fake_fetch_version  # ty: ignore[invalid-assignment] # reason: test double
    archive_client.fetch_office_summary = fake_fetch_office_summary  # ty: ignore[invalid-assignment] # reason: test double
    archive_client.fetch_county_detail = fake_fetch_county_detail  # ty: ignore[invalid-assignment] # reason: test double

    bundle = await archive_client.fetch_election_bundle(election_ref)
    assert bundle.county_detail is None
    assert bundle.office_summary == {"OS": []}


@pytest.mark.asyncio
async def test_fetch_election_bundle_includes_county_detail() -> None:
    archive_client = CivixArchiveClient(pacing_seconds=0)
    election_ref = CivixElectionRef(
        election_id="49664",
        name="2024 NOVEMBER 5TH GENERAL ELECTION",
        year="2024",
        election_type_code="G",
        election_class_code="SW",
    )
    county_payload = {"48001": {"N": "ANDERSON", "Races": {}}}

    async def fake_fetch_version(election_id: str) -> object:
        assert election_id == "49664"
        return parse_election_version("49664", {"___versionNo": 1012, "elecDate": "11052024"})

    async def fake_fetch_office_summary(election_id: str, version_no: int) -> dict:
        assert election_id == "49664" and version_no == 1012
        return {"OS": []}

    async def fake_fetch_county_detail(election_id: str, version_no: int) -> dict:
        assert election_id == "49664" and version_no == 1012
        return county_payload

    archive_client.fetch_election_version = fake_fetch_version  # ty: ignore[invalid-assignment] # reason: test double
    archive_client.fetch_office_summary = fake_fetch_office_summary  # ty: ignore[invalid-assignment] # reason: test double
    archive_client.fetch_county_detail = fake_fetch_county_detail  # ty: ignore[invalid-assignment] # reason: test double

    bundle = await archive_client.fetch_election_bundle(election_ref)
    assert bundle.county_detail == county_payload

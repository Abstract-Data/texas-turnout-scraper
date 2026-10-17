"""Unit tests for Civix ENR decode, discovery, and retry. Ported from Quick Results."""

from __future__ import annotations

import base64
import json
from datetime import timezone

import httpx
import pytest

from texas_turnout_scraper.civix.enr import (
    _fetch_json,
    decode_election_constants,
    decode_election_payload,
    discover_election_refs,
    json_payload_hash,
    parse_civix_updated_time,
)
from texas_turnout_scraper.civix.ids import normalize_election_id
from texas_turnout_scraper.civix.transport import fetch_json


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("texas_turnout_scraper.http_transport._json_retry_sleep", _sleep)


def _b64(value: dict) -> str:
    return base64.b64encode(json.dumps(value).encode("utf-8")).decode("utf-8")


def test_parse_civix_updated_time_parses_display_format() -> None:
    """Civix display timestamp format parses into a UTC-aware datetime."""
    parsed = parse_civix_updated_time("Feb 28, 2026 22:36:56")
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 2
    assert parsed.day == 28
    assert parsed.tzinfo == timezone.utc


def test_parse_civix_updated_time_empty_is_none() -> None:
    assert parse_civix_updated_time("  ") is None
    assert parse_civix_updated_time(None) is None


def test_decode_election_payload_decodes_base64_sections() -> None:
    """Election payload decoder should decode all non-Version keys."""
    raw = {
        "Version": "enr/53814/5/",
        "Home": _b64({"LastUpdatedTime": "Feb 28, 2026 22:36:56"}),
        "Federal": _b64({"OffType": "FEDERAL OFFICES", "Races": []}),
    }
    decoded = decode_election_payload(raw)
    assert decoded["Version"] == "enr/53814/5/"
    assert decoded["Home"]["LastUpdatedTime"] == "Feb 28, 2026 22:36:56"
    assert decoded["Federal"]["OffType"] == "FEDERAL OFFICES"


def test_decode_election_payload_bad_section_becomes_empty_object() -> None:
    decoded = decode_election_payload({"Version": "v", "Home": "not-base64"})
    assert decoded["Version"] == "v"
    assert decoded["Home"] == {}


def test_discover_election_refs_filters_by_year_and_type() -> None:
    """Election discovery returns refs filtered by year and type codes."""
    constants_payload = {
        "upload": _b64(
            {
                "electionInfo": {
                    "2026": {
                        "P": {
                            "53813": {
                                "ID": 53813,
                                "N": "2026 REPUBLICAN PRIMARY ELECTION",
                                "Y": "2026",
                                "ET": "P",
                                "EC": "SW",
                            }
                        },
                        "SR": {
                            "54613": {
                                "ID": 54613,
                                "N": "2026 SPECIAL RUNOFF ELECTION SENATE DISTRICT 9",
                                "Y": "2026",
                                "ET": "SR",
                                "EC": "PS",
                            }
                        },
                    }
                }
            }
        )
    }
    decoded_constants = decode_election_constants(constants_payload)
    refs = discover_election_refs(
        decoded_constants,
        year="2026",
        type_codes={"P"},
        max_items=10,
    )
    assert len(refs) == 1
    assert refs[0].election_id == "53813"
    assert isinstance(refs[0].election_id, str)
    assert refs[0].election_type_code == "P"


def test_normalize_election_id_keeps_leading_zeros() -> None:
    assert normalize_election_id("0123456789") == "0123456789"
    assert normalize_election_id(49664) == "49664"
    assert normalize_election_id(True) is None
    assert normalize_election_id(0) is None


def test_json_payload_hash_is_stable() -> None:
    assert json_payload_hash({"b": 1, "a": 2}) == json_payload_hash({"a": 2, "b": 1})


class _SequencedClient:
    """Deterministic fake async client that returns configured outcomes."""

    def __init__(self, outcomes: list[str | tuple[int, dict | list]]) -> None:
        self._outcomes = outcomes
        self.calls = 0
        self.urls: list[str] = []

    async def get(self, url: str, *, timeout: int, headers: dict[str, str]) -> httpx.Response:
        self.calls += 1
        self.urls.append(url)
        outcome = self._outcomes[self.calls - 1]
        request = httpx.Request("GET", url)
        if outcome == "timeout":
            raise httpx.ReadTimeout("temporary timeout", request=request)
        if outcome == "connect_error":
            raise httpx.ConnectError("temporary connect failure", request=request)
        status_code, payload = outcome
        return httpx.Response(int(status_code), json=payload, request=request)


@pytest.mark.asyncio
async def test_fetch_json_retries_on_transient_timeout_then_succeeds() -> None:
    """Transient timeout should be retried and eventually succeed."""
    client = _SequencedClient(["timeout", (200, {"ok": True})])
    payload = await _fetch_json("/s3/enr/electionConstants", client=client)
    assert payload == {"ok": True}
    assert client.calls == 2


@pytest.mark.asyncio
async def test_fetch_json_retries_on_retryable_http_status_then_succeeds() -> None:
    """Retryable HTTP statuses (5xx/429) should trigger bounded retries."""
    client = _SequencedClient([(503, {"error": "upstream unavailable"}), (200, {"ok": True})])
    payload = await _fetch_json("/s3/enr/election/12345", client=client)
    assert payload == {"ok": True}
    assert client.calls == 2
    assert client.urls[0].endswith("/s3/enr/election/12345")


@pytest.mark.asyncio
async def test_fetch_json_retries_http_429() -> None:
    client = _SequencedClient([(429, {"error": "slow down"}), (200, {"ok": True})])
    payload = await _fetch_json("/s3/enr/electionConstants", client=client)
    assert payload == {"ok": True}
    assert client.calls == 2


@pytest.mark.asyncio
async def test_fetch_json_does_not_retry_non_retryable_http_status() -> None:
    """Non-retryable HTTP statuses should fail fast without retry."""
    client = _SequencedClient([(404, {"error": "not found"})])
    with pytest.raises(httpx.HTTPStatusError):
        await _fetch_json("/s3/enr/election/does-not-exist", client=client)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_fetch_json_stops_after_bounded_retry_attempts() -> None:
    """Transient failures should stop at the configured retry bound."""
    client = _SequencedClient(["connect_error", "timeout", "timeout"])
    with pytest.raises(httpx.TimeoutException):
        await _fetch_json("/s3/enr/electionConstants", client=client)
    assert client.calls == 3


@pytest.mark.asyncio
async def test_fetch_json_non_object_json_is_empty_object() -> None:
    client = _SequencedClient([(200, [1, 2])])
    assert await _fetch_json("/s3/enr/electionConstants", client=client) == {}


@pytest.mark.asyncio
async def test_fetch_json_injects_base_url() -> None:
    client = _SequencedClient([(200, {"ok": True})])
    payload = await _fetch_json(
        "/s3/enr/electionConstants",
        client=client,
        base_url="https://example.test/api/",
    )
    assert payload == {"ok": True}
    assert client.urls == ["https://example.test/api/s3/enr/electionConstants"]


@pytest.mark.asyncio
async def test_owned_client_receives_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, *, timeout: float, headers: dict[str, str]) -> httpx.Response:
            seen["url"] = url
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr("texas_turnout_scraper.civix.transport.httpx.AsyncClient", _Client)
    payload = await fetch_json("/s3/enr/electionConstants", base_url="https://example.test/api")
    assert payload == {"ok": True}
    assert seen["base_url"] == "https://example.test/api"
    assert seen["url"] == "https://example.test/api/s3/enr/electionConstants"

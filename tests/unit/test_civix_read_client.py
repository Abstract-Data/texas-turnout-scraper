"""Contract tests for the swappable early-vote read client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import httpx
import pytest
from pydantic import ValidationError

from texas_turnout_scraper.civix import EarlyVoteReadClient as exported_read_client
from texas_turnout_scraper.civix.read_client import (
    EV_API_BINDING,
    PURGE_OLD_VERSIONS,
    EarlyVoteReadClient,
    HttpxReadTransport,
    ReadClientError,
    ReadResult,
    TransportResponse,
    cache_key,
    etag_for_version,
    etag_matches,
    http_path,
    queries_d1,
    version_from_etag,
    version_key,
)


class _FakeTransport:
    def __init__(self, response: TransportResponse) -> None:
        self.response = response
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.calls = 0

    async def send(self, path: str, *, headers: Mapping[str, str]) -> TransportResponse:
        self.calls += 1
        self.paths.append(path)
        self.headers.append(dict(headers))
        return self.response


def test_package_reexports_read_client() -> None:
    assert exported_read_client is EarlyVoteReadClient


def test_binding_name_and_no_purge() -> None:
    assert EV_API_BINDING == "EV_API"
    assert PURGE_OLD_VERSIONS is False


def test_version_and_cache_keys_keep_election_ids_as_strings() -> None:
    assert version_key("049664") == "ev:049664:version"
    assert version_key(49664) == "ev:49664:version"
    assert cache_key("49664", 42, "county", "453") == "ev:49664:v42:county:453"
    assert cache_key("049664", 42, "county", "001") == "ev:049664:v42:county:001"
    assert http_path("49664", "county", "453") == "/elections/49664/county/453"


@pytest.mark.parametrize(
    ("election_id", "version", "segments"),
    [
        (True, 1, ("county", "453")),
        ("49664", True, ("county", "453")),
        ("49664", 0, ("county", "453")),
        ("49664", 1, ()),
        ("49664", 1, ("county/453",)),
        ("49664", 1, (453,)),
    ],
)
def test_cache_key_rejects_bad_ids_versions_and_segments(
    election_id: object,
    version: object,
    segments: tuple[object, ...],
) -> None:
    with pytest.raises((ValueError, TypeError)):
        cache_key(election_id, version, *segments)


def test_etag_is_tied_to_the_version() -> None:
    assert etag_for_version(42) == '"v42"'
    assert version_from_etag('"v42"') == 42
    assert version_from_etag('W/"v42"') == 42
    assert etag_matches('"v42"', '"v42"') is True
    assert etag_matches('W/"v42"', '"v42"') is True
    assert etag_matches('"v41", "v42"', '"v42"') is True
    assert etag_matches("*", '"v42"') is True
    assert etag_matches('"v41"', '"v42"') is False
    assert etag_matches(None, '"v42"') is False
    assert etag_matches("", '"v42"') is False
    with pytest.raises(ValueError):
        etag_for_version(True)
    with pytest.raises(ValueError):
        version_from_etag('"election"')
    with pytest.raises(ValueError):
        etag_matches("v42", '"v42"')


def test_d1_is_queried_only_on_a_cache_miss() -> None:
    assert queries_d1(cache_hit=True) is False
    assert queries_d1(cache_hit=False) is True
    with pytest.raises(TypeError):
        queries_d1(cache_hit=cast(bool, 1))


async def test_get_uses_the_injected_transport_and_keeps_string_ids() -> None:
    transport = _FakeTransport(
        TransportResponse(status_code=200, headers={"ETag": '"v7"'}, body=b'{"rows":2}')
    )
    result = await EarlyVoteReadClient(transport).get(49664, "county", "453")

    assert transport.paths == ["/elections/49664/county/453"]
    assert transport.headers == [{}]
    assert result.election_id == "49664"
    assert result.segments == ("county", "453")
    assert result.not_modified is False
    assert result.etag == '"v7"'
    assert result.body == {"rows": 2}
    with pytest.raises(ValidationError):
        result.election_id = "1"  # type: ignore[misc]


async def test_get_preserves_leading_zeros() -> None:
    transport = _FakeTransport(
        TransportResponse(status_code=200, headers={"etag": '"v1"'}, body=b'{"rows":0}')
    )
    result = await EarlyVoteReadClient(transport).get("049664", "county", "001")

    assert transport.paths == ["/elections/049664/county/001"]
    assert result.election_id == "049664"


async def test_304_does_not_parse_the_body() -> None:
    transport = _FakeTransport(
        TransportResponse(
            status_code=304,
            headers={"ETag": '"v7"'},
            body=b'{"VOTER_NAME":"TEST_NAME","ID_VOTER":"0000000000"}',
        )
    )
    result = await EarlyVoteReadClient(transport).get(
        "49664",
        "county",
        "453",
        if_none_match='"v7"',
    )

    assert transport.headers == [{"If-None-Match": '"v7"'}]
    assert result == ReadResult(
        election_id="49664",
        segments=("county", "453"),
        status_code=304,
        etag='"v7"',
        not_modified=True,
        body=None,
    )


async def test_error_message_omits_response_body() -> None:
    transport = _FakeTransport(
        TransportResponse(
            status_code=500,
            headers={},
            body=b'{"VOTER_NAME":"TEST_NAME","ID_VOTER":"0000000000"}',
        )
    )
    with pytest.raises(ReadClientError) as caught:
        await EarlyVoteReadClient(transport).get("49664", "county", "453")

    message = str(caught.value)
    assert caught.value.status_code == 500
    assert caught.value.path == "/elections/49664/county/453"
    assert "TEST_NAME" not in message
    assert "0000000000" not in message
    assert "VOTER_NAME" not in message
    assert "ID_VOTER" not in message


async def test_non_object_json_is_rejected_without_the_payload() -> None:
    transport = _FakeTransport(
        TransportResponse(status_code=200, headers={}, body=b'["TEST_NAME"]')
    )
    with pytest.raises(ReadClientError) as caught:
        await EarlyVoteReadClient(transport).get("49664", "county", "453")

    assert "TEST_NAME" not in str(caught.value)
    assert caught.value.reason == "response was not a JSON object"


async def test_bad_election_id_does_not_call_the_transport() -> None:
    transport = _FakeTransport(TransportResponse(status_code=200, headers={}, body=b"{}"))
    with pytest.raises(ValueError):
        await EarlyVoteReadClient(transport).get(True, "county", "453")
    assert transport.calls == 0


def test_access_token_requires_both_halves_and_does_not_echo_them() -> None:
    with pytest.raises(ValueError, match="Access service token") as caught:
        HttpxReadTransport(
            base_url="https://preview.example.test",
            access_client_id="only-id",
        )
    assert "only-id" not in str(caught.value)


async def test_httpx_transport_sends_conditional_get_and_maps_304() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["if_none_match"] = request.headers["If-None-Match"]
        seen["access_id"] = request.headers["CF-Access-Client-Id"]
        seen["access_secret"] = request.headers["CF-Access-Client-Secret"]
        return httpx.Response(
            304,
            headers={"ETag": '"v42"'},
            content=b'{"VOTER_NAME":"TEST_NAME"}',
        )

    mock = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=mock) as http:
        client = EarlyVoteReadClient(
            HttpxReadTransport(
                base_url="https://preview.example.test/api",
                client=http,
                access_client_id="cf-id",
                access_client_secret="cf-secret-value",
            )
        )
        result = await client.get("49664", "county", "453", if_none_match='"v42"')

    assert seen["url"] == "https://preview.example.test/api/elections/49664/county/453"
    assert seen["if_none_match"] == '"v42"'
    assert seen["access_id"] == "cf-id"
    assert seen["access_secret"] == "cf-secret-value"
    assert result.not_modified is True
    assert result.body is None
    assert result.etag == '"v42"'


async def test_httpx_error_omits_body_and_access_secret() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text='{"VOTER_NAME":"TEST_NAME","ID_VOTER":"0000000000"}',
        )

    mock = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=mock) as http:
        client = EarlyVoteReadClient(
            HttpxReadTransport(
                base_url="https://preview.example.test",
                client=http,
                access_client_id="cf-id",
                access_client_secret="cf-secret-value",
            )
        )
        with pytest.raises(ReadClientError) as caught:
            await client.get("49664", "county", "453")

    message = str(caught.value)
    assert "TEST_NAME" not in message
    assert "0000000000" not in message
    assert "cf-secret-value" not in message
    assert "cf-id" not in message

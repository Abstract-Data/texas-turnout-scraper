"""Unit tests for http_transport.PacedHttpClient — httpx backend via respx."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from texas_turnout_scraper.http_transport import PacedHttpClient

_BASE = "https://earlyvoting.example.com"


def test_invalid_backend_raises_value_error() -> None:
    with pytest.raises(ValueError, match=r"http_backend must be 'httpx' or 'cloudscraper'"):
        PacedHttpClient(_BASE, backend="selenium")  # type: ignore[arg-type]


def test_backend_property_httpx() -> None:
    client = PacedHttpClient(_BASE, backend="httpx")
    try:
        assert client.backend == "httpx"
    finally:
        client.close()


@respx.mock
def test_get_and_post_httpx() -> None:
    respx.get(f"{_BASE}/Elections/getElectionDetails.do").mock(
        return_value=httpx.Response(200, text="ok-get")
    )
    respx.post(f"{_BASE}/Elections/getElectionEVDates.do").mock(
        return_value=httpx.Response(200, text="ok-post")
    )

    client = PacedHttpClient(_BASE, backend="httpx")
    try:
        get_resp = client.get("/Elections/getElectionDetails.do")
        post_resp = client.post(
            "/Elections/getElectionEVDates.do",
            data={"idElection": "49664"},
        )
        assert get_resp.text == "ok-get"
        assert post_resp.text == "ok-post"
        assert len(respx.calls) == 2
    finally:
        client.close()


@respx.mock
def test_cookies_persist_after_set_cookie() -> None:
    respx.get(f"{_BASE}/session/ping").mock(
        return_value=httpx.Response(
            200,
            text="ok",
            headers=[("Set-Cookie", "JSESSIONID=abc123def; Path=/; HttpOnly")],
        )
    )

    client = PacedHttpClient(_BASE, backend="httpx")
    try:
        client.get("/session/ping")
        assert client.cookies.get("JSESSIONID") == "abc123def"
    finally:
        client.close()


@respx.mock
def test_stream_iter_bytes_httpx() -> None:
    payload = b"PK\x03\x04" + b"fake-zip-bytes"
    route = respx.post(f"{_BASE}/Elections/downloadVoterInfoReport.do").mock(
        return_value=httpx.Response(200, content=payload)
    )

    client = PacedHttpClient(_BASE, backend="httpx")
    try:
        with client.stream(
            "POST",
            "/Elections/downloadVoterInfoReport.do",
            data={"idElection": "49664"},
        ) as resp:
            resp.raise_for_status()
            chunks = list(resp.iter_bytes(chunk_size=8))

        assert b"".join(chunks) == payload
        assert route.called
    finally:
        client.close()


def test_stream_iter_bytes_cloudscraper_adapter() -> None:
    """cloudscraper path uses requests stream + _RequestsStreamAdapter.iter_bytes."""
    mock_response = MagicMock()
    mock_response.iter_content.return_value = [b"PK\x03\x04", b"zip", b""]

    mock_scraper = MagicMock()
    mock_scraper.request.return_value = mock_response
    mock_scraper.headers = {}

    with patch("cloudscraper.create_scraper", return_value=mock_scraper):
        client = PacedHttpClient(_BASE, backend="cloudscraper")
        try:
            with client.stream("POST", "/Elections/downloadVoterInfoReport.do") as resp:
                chunks = list(resp.iter_bytes(chunk_size=4))

            assert chunks == [b"PK\x03\x04", b"zip"]
            mock_scraper.request.assert_called_once()
            mock_response.close.assert_called_once()
        finally:
            client.close()

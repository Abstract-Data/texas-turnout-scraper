"""Temporary spike route: streaming envelope decode, guards, auth and the report shape."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import random
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from ev_api import spike_portal
from ev_api.app import ReadDeps, create_app
from ev_api.auth import AuthDenied, scope_dependency
from ev_api.spike_portal import (
    EnvelopeError,
    EnvelopeStreamDecoder,
    body_chunks,
    looks_like_html_challenge,
    portal_url,
    probe_portal,
    register_spike_routes,
)
from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from tests.unit.test_early_vote_roster_stream import DAY1, _Db, _store_day
from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.consumer_tokens import (
    SCOPE_AGGREGATES,
    hash_token,
    kv_key_for_hash,
)
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES

BEARER = {"Authorization": "Bearer agg-secret"}
PREFIX = b'{"upload":"'
MARKER = b"SYNTHETIC-VOTER-NAME"
TODAY = date(2026, 10, 25)
URL = "/spike/portal-fetch?election_id=53815&date=2026-10-20"


def envelope_for(raw: bytes) -> tuple[bytes, str]:
    return PREFIX + base64.b64encode(raw) + b'"}', hashlib.sha256(raw).hexdigest()


def synthetic_raw(size: int, seed: int = 7) -> bytes:
    return MARKER + random.Random(seed).randbytes(size)


def split_at(data: bytes, cuts: list[int]) -> list[bytes]:
    points = [0, *sorted(set(cuts)), len(data)]
    return [data[a:b] for a, b in itertools.pairwise(points) if b > a]


def random_cuts(length: int, seed: int, pieces: int) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(1, length) for _ in range(pieces)]


def decode(chunks: list[bytes]) -> EnvelopeStreamDecoder:
    decoder = EnvelopeStreamDecoder()
    for piece in chunks:
        decoder.feed(piece)
    decoder.finish()
    return decoder


def test_multi_megabyte_stream_equals_whole_body_decode() -> None:
    raw = synthetic_raw(4 * 1024 * 1024 + 1)
    body, expected_sha = envelope_for(raw)
    assert len(body) > 5_000_000
    group_start = len(PREFIX) + 4096 + 2
    cuts = [4, len(PREFIX) - 1, len(PREFIX), group_start, group_start + 1]
    cuts += [*random_cuts(len(body), seed=11, pieces=300), len(body) - 2, len(body) - 1]
    decoder = decode(split_at(body, cuts))
    whole = base64.b64decode(body[len(PREFIX) : -2])
    assert decoder.decoded_bytes == len(whole) == len(raw)
    assert decoder.decoded_sha256 == hashlib.sha256(whole).hexdigest() == expected_sha
    assert decoder.envelope_chars == len(body) - len(PREFIX) - 2
    assert decoder.chunks == len(split_at(body, cuts))


@pytest.mark.parametrize("raw_size", [0, 1, 2, 3, 4, 5, 17])
def test_every_two_way_split_and_byte_at_a_time(raw_size: int) -> None:
    raw = synthetic_raw(raw_size)
    body, expected_sha = envelope_for(raw)
    for cut in range(len(body) + 1):
        decoder = decode([body[:cut], body[cut:]])
        assert decoder.decoded_sha256 == expected_sha
        assert decoder.decoded_bytes == len(raw)
    one_by_one = decode([body[i : i + 1] for i in range(len(body))])
    assert one_by_one.decoded_sha256 == expected_sha


@pytest.mark.parametrize(
    "variant",
    [b'  {\n "upload" : "', b'{"upload":"'],
    ids=["spaced", "compact"],
)
def test_prefix_whitespace_variants(variant: bytes) -> None:
    raw = synthetic_raw(40)
    body = variant + base64.b64encode(raw) + b'"\n}'
    for cut in range(len(body) + 1):
        assert decode([body[:cut], body[cut:]]).decoded_sha256 == hashlib.sha256(raw).hexdigest()


def test_json_escaped_slash_and_padding_split_across_chunks() -> None:
    raw = bytes(range(250, 256)) * 9 + b"?>?>" + MARKER + b"!"
    text = base64.b64encode(raw)
    assert b"/" in text and text.endswith(b"=")
    escaped = text.replace(b"/", b"\\/").replace(b"=", b"\\u003d")
    body = PREFIX + escaped + b'"}'
    for cut in range(len(body) + 1):
        decoder = decode([body[:cut], body[cut:]])
        assert decoder.decoded_sha256 == hashlib.sha256(raw).hexdigest()
    assert decode([body[i : i + 1] for i in range(len(body))]).decoded_bytes == len(raw)


def test_malformed_bodies_report_a_reason() -> None:
    def reason(chunks: list[bytes]) -> str:
        with pytest.raises(EnvelopeError) as caught:
            decode(chunks)
        return caught.value.reason

    assert reason([b"<html>" + b"x" * 200]) == "bad_prefix"
    assert reason([b'{"up']) == "bad_prefix"
    assert reason([PREFIX + b"QUJD"]) == "truncated"
    assert reason([PREFIX + b'QUJD!"}']) == "bad_base64"
    assert reason([PREFIX + b'QUJ"}']) == "bad_base64"
    assert reason([PREFIX + b'QU\\n"}']) == "unsupported_escape"


def test_decoded_cap_stops_a_runaway_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spike_portal, "_MAX_DECODED_BYTES", 10)
    body, _ = envelope_for(b"x" * 64)
    with pytest.raises(EnvelopeError) as caught:
        decode([body])
    assert caught.value.reason == "too_large"


class FakeBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.pulled = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        for piece in self.chunks:
            self.pulled += 1
            yield piece


class FakeJsStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.cancelled = False
        self.pulled = 0

    def getReader(self) -> FakeJsStream:
        return self

    async def read(self) -> SimpleNamespace:
        if not self.chunks:
            return SimpleNamespace(done=True, value=None)
        self.pulled += 1
        piece = self.chunks.pop(0)
        return SimpleNamespace(done=False, value=SimpleNamespace(to_bytes=lambda: piece))

    async def cancel(self) -> None:
        self.cancelled = True


async def test_js_reader_and_async_iterable_yield_the_same_chunks() -> None:
    pieces = [b"ab", b"", b"cde"]
    via_iterable = [piece async for piece in body_chunks(FakeBody(pieces))]
    stream = FakeJsStream([p for p in pieces if p])
    via_reader = [piece async for piece in body_chunks(stream)]
    assert via_iterable == pieces
    assert via_reader == [b"ab", b"cde"]
    assert not stream.cancelled


async def test_js_reader_is_cancelled_when_the_consumer_stops_early() -> None:
    stream = FakeJsStream([b"a", b"b", b"c"])
    pieces = body_chunks(stream)
    assert await pieces.__anext__() == b"a"
    await pieces.aclose()
    assert stream.cancelled


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: Any) -> None:
        self.status = status
        self.headers = headers
        self.body = body


def json_headers(**extra: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "server": "cloudflare",
        "cf-ray": "abc123-DFW",
        **extra,
    }


def ok_response(raw: bytes, *, pieces: int = 40, seed: int = 3) -> FakeResponse:
    body, _ = envelope_for(raw)
    chunks = split_at(body, random_cuts(len(body), seed, pieces))
    return FakeResponse(200, json_headers(**{"content-length": str(len(body))}), FakeBody(chunks))


class Recorder:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeResponse:
        self.calls.append((url, headers))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Kv:
    def __init__(self) -> None:
        self.values = {kv_key_for_hash(hash_token("agg-secret")): "aggregates"}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class Rosters:
    def __init__(self, dbs: dict[str, Any]) -> None:
        self._dbs = dbs

    def election_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._dbs))

    def database(self, election_id: str) -> Any:
        return self._dbs.get(election_id)


def make_deps(rosters: Rosters | None = None) -> ReadDeps:
    return ReadDeps(
        kv=Kv(),
        store=None,  # type: ignore[arg-type]
        rosters=rosters or Rosters({}),
    )


def app_behind_auth(active: ReadDeps) -> FastAPI:
    app = FastAPI(
        dependencies=[Depends(scope_dependency(lambda request: active, SCOPE_AGGREGATES))]
    )

    @app.exception_handler(AuthDenied)
    async def auth_denied(request: Request, exc: AuthDenied) -> Response:
        return exc.response

    return app


def spike_client(
    fetch: Callable[..., Any],
    *,
    deps: ReadDeps | None = None,
    clock: Clock | None = None,
    today: date = TODAY,
) -> TestClient:
    active = deps or make_deps()
    app = app_behind_auth(active)
    register_spike_routes(
        app, lambda request: active, fetch=fetch, clock=clock or Clock(), today=lambda: today
    )
    return TestClient(app)


@pytest.fixture
def stored_db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    for sql in _SCHEMA_CREATES:
        conn.execute(sql)
    _store_day(conn, "2026-10-20", DAY1)
    return _Db(conn)


def test_report_for_a_complete_download_has_counts_and_hashes_only() -> None:
    raw = synthetic_raw(2 * 1024 * 1024)
    fetch = Recorder(ok_response(raw))
    client = spike_client(fetch)
    response = client.get(URL, headers=BEARER)
    assert response.status_code == 200
    report = response.json()
    assert report["http_status"] == 200
    assert report["decoded_bytes"] == len(raw)
    assert report["decoded_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["envelope_chars"] == len(base64.b64encode(raw))
    assert report["chunks"] == 41
    assert report["body_looks_like_html_challenge"] is False
    assert report["error"] is None
    assert report["response_headers"]["cf-ray-present"] is True
    assert report["response_headers"]["server"] == "cloudflare"
    assert report["response_headers"]["cf-mitigated"] is None
    assert isinstance(report["ttfb_ms"], int) and isinstance(report["total_ms"], int)
    assert report["peak_memory_note"] and report["timing_note"]
    assert report["stored_hash_is_over_canonical_bytes"] is True
    assert report["stored_sha256"] is None
    assert report["stored_matches_raw_decoded"] is None
    assert MARKER.decode() not in response.text
    assert response.headers["cache-control"] == "no-store"
    ((url, headers),) = fetch.calls
    assert url == (
        "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
        "?type=EVR_STATEWIDE&electionId=53815&electionDate=10/20/2026"
    )
    assert headers["User-Agent"] == spike_portal.SPIKE_USER_AGENT


def test_stored_hash_comparison_uses_the_d1_day_row(stored_db: _Db) -> None:
    stored = hashlib.sha256(DAY1).hexdigest()
    deps = make_deps(Rosters({"53815": stored_db}))

    same = spike_client(Recorder(ok_response(DAY1)), deps=deps).get(URL, headers=BEARER).json()
    assert same["stored_sha256"] == stored
    assert same["decoded_sha256"] == stored
    assert same["stored_matches_raw_decoded"] is True

    other = spike_client(Recorder(ok_response(DAY1 + b"\r\n")), deps=deps)
    differs = other.get(URL, headers=BEARER).json()
    assert differs["stored_sha256"] == stored
    assert differs["stored_matches_raw_decoded"] is False
    assert differs["stored_hash_is_over_canonical_bytes"] is True

    missing = spike_client(Recorder(ok_response(DAY1)), deps=deps)
    no_day = missing.get(URL.replace("10-20", "10-21"), headers=BEARER).json()
    assert no_day["stored_sha256"] is None
    assert no_day["stored_matches_raw_decoded"] is None


async def test_challenge_html_with_200_is_flagged_and_not_decoded() -> None:
    page = b"<!DOCTYPE html><html><title>Just a moment...</title>" + b"x" * 5000
    body = FakeBody([page[:100], page[100:]])
    fetch = Recorder(FakeResponse(200, json_headers(**{"cf-mitigated": "challenge"}), body))
    report = await probe_portal(fetch, "https://example.invalid/")
    assert report["body_looks_like_html_challenge"] is True
    assert report["decoded_bytes"] == 0
    assert report["decoded_sha256"] is None
    assert report["error"] is None
    assert report["response_headers"]["cf-mitigated"] == "challenge"
    assert body.pulled == 1


async def test_html_content_type_is_flagged_even_when_the_body_looks_plain() -> None:
    headers = json_headers()
    headers["content-type"] = "text/html; charset=utf-8"
    fetch = Recorder(FakeResponse(200, headers, FakeBody([b"plain text body"])))
    report = await probe_portal(fetch, "https://example.invalid/")
    assert report["body_looks_like_html_challenge"] is True
    assert report["decoded_sha256"] is None


def test_non_200_is_reported_with_headers_and_the_body_is_not_read_past_the_first_chunk() -> None:
    body = FakeBody([b"<html>blocked</html>", b"more", b"more"])
    headers = json_headers(**{"retry-after": "30", "cf-mitigated": "challenge"})
    headers["content-type"] = "text/html"
    client = spike_client(Recorder(FakeResponse(403, headers, body)))
    report = client.get(URL, headers=BEARER).json()
    assert report["http_status"] == 403
    assert report["response_headers"]["retry-after"] == "30"
    assert report["response_headers"]["cf-mitigated"] == "challenge"
    assert report["body_looks_like_html_challenge"] is True
    assert report["decoded_bytes"] == 0
    assert report["decoded_sha256"] is None
    assert body.pulled == 1


def test_non_200_json_error_is_not_a_challenge() -> None:
    fetch = Recorder(FakeResponse(503, json_headers(), FakeBody([b'{"error":"busy"}'])))
    report = spike_client(fetch).get(URL, headers=BEARER).json()
    assert report["http_status"] == 503
    assert report["body_looks_like_html_challenge"] is False
    assert report["decoded_sha256"] is None


def test_truncated_body_reports_the_error_class_and_reason_only() -> None:
    body, _ = envelope_for(synthetic_raw(1000))
    truncated = FakeBody([body[:300]])
    report = spike_client(Recorder(FakeResponse(200, json_headers(), truncated)))
    result = report.get(URL, headers=BEARER).json()
    assert result["error"] == {"class": "EnvelopeError", "reason": "truncated"}
    assert result["decoded_sha256"] is None
    assert result["decoded_bytes"] > 0


def test_fetch_failure_reports_the_exception_class_without_its_message() -> None:
    failure = OSError("connect failed for SYNTHETIC-SECRET-DETAIL")
    response = spike_client(Recorder(failure)).get(URL, headers=BEARER)
    assert response.status_code == 200
    report = response.json()
    assert report["error"] == {"class": "OSError"}
    assert report["http_status"] is None
    assert report["decoded_sha256"] is None
    assert "SYNTHETIC-SECRET-DETAIL" not in response.text


async def test_probe_converts_a_stream_error_into_a_class_only_report() -> None:
    class Exploding:
        def __aiter__(self) -> AsyncIterator[bytes]:
            return self._iterate()

        async def _iterate(self) -> AsyncIterator[bytes]:
            yield PREFIX + b"QUJD"
            raise ConnectionResetError("SYNTHETIC-SECRET-DETAIL")

    fetch = Recorder(FakeResponse(200, json_headers(), Exploding()))
    report = await probe_portal(fetch, "https://example.invalid/")
    assert report["error"] == {"class": "ConnectionResetError"}
    assert "SYNTHETIC-SECRET-DETAIL" not in json.dumps(report)
    assert report["decoded_sha256"] is None


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"X-API-Key": "nope"}])
def test_unauthenticated_calls_are_refused_before_the_portal_is_touched(
    headers: dict[str, str],
) -> None:
    fetch = Recorder(ok_response(synthetic_raw(10)))
    client = spike_client(fetch)
    assert client.get(URL, headers=headers).status_code == 401
    assert fetch.calls == []


def test_a_consumer_bearer_token_is_accepted() -> None:
    fetch = Recorder(ok_response(synthetic_raw(10)))
    assert spike_client(fetch).get(URL, headers=BEARER).status_code == 200
    assert len(fetch.calls) == 1


def test_the_mounted_app_route_sits_behind_the_same_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str, headers: dict[str, str]) -> FakeResponse:
        calls.append(url)
        return ok_response(synthetic_raw(10))

    monkeypatch.setattr(spike_portal, "_workers_fetch", fake_fetch)
    monkeypatch.setattr(spike_portal, "_utc_today", lambda: TODAY)
    client = TestClient(create_app(make_deps()))
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health").status_code == 401
    assert calls == []
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert "/spike/portal-fetch" in paths


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("election_id=53815&date=2026-10-26", "date_in_future"),
        ("election_id=53815&date=2027-01-01", "date_in_future"),
        ("election_id=53815&date=2026-10-13", "date_outside_calendar"),
        ("election_id=99999&date=2026-10-20", "election_not_allowed"),
        ("election_id=abc&date=2026-10-20", "bad_request"),
        ("election_id=53815&date=10/20/2026", "bad_request"),
        ("election_id=53815&date=2026-02-30", "bad_request"),
        ("election_id=53815", "bad_request"),
        ("date=2026-10-20", "bad_request"),
    ],
)
def test_future_out_of_calendar_and_malformed_requests_never_reach_the_portal(
    query: str, detail: str
) -> None:
    fetch = Recorder(ok_response(synthetic_raw(10)))
    response = spike_client(fetch).get(f"/spike/portal-fetch?{query}", headers=BEARER)
    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    assert fetch.calls == []


def test_the_last_calendar_day_is_allowed_once_it_is_not_in_the_future() -> None:
    fetch = Recorder(ok_response(synthetic_raw(10)))
    client = spike_client(fetch, today=date(2026, 10, 30))
    assert client.get(URL.replace("10-20", "10-30"), headers=BEARER).status_code == 200


def test_one_call_per_ten_seconds_per_isolate() -> None:
    clock = Clock()
    fetch = Recorder(ok_response(synthetic_raw(10)))
    client = spike_client(fetch, clock=clock)
    assert client.get(URL, headers=BEARER).status_code == 200
    clock.now += 3
    refused = client.get(URL, headers=BEARER)
    assert refused.status_code == 429
    assert refused.json() == {"detail": "rate_limited"}
    assert refused.headers["retry-after"] == "8"
    assert len(fetch.calls) == 1
    clock.now += 7
    fetch.response = ok_response(synthetic_raw(10))
    assert client.get(URL, headers=BEARER).status_code == 200
    assert len(fetch.calls) == 2


def test_rejected_and_unauthenticated_requests_do_not_use_up_the_slot() -> None:
    fetch = Recorder(ok_response(synthetic_raw(10)))
    client = spike_client(fetch)
    assert client.get(URL).status_code == 401
    assert client.get(URL.replace("2026-10-20", "2026-12-01"), headers=BEARER).status_code == 400
    assert client.get(URL, headers=BEARER).status_code == 200


def test_challenge_detector_ignores_base64_text() -> None:
    body, _ = envelope_for(random.Random(5).randbytes(600))
    assert looks_like_html_challenge("application/json", body[:512]) is False
    assert looks_like_html_challenge(None, b"<HTML><body>") is True


def test_portal_url_formats_the_date_like_the_sync() -> None:
    assert portal_url("53815", date(2026, 10, 9)).endswith("electionDate=10/09/2026")

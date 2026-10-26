"""Temporary spike route: can the Worker download and decode a statewide roster day file?

``GET /spike/portal-fetch?election_id=53815&date=YYYY-MM-DD`` makes one request to the
Civix portal, streams the ``{"upload":"<base64>"}`` envelope through an incremental base64
decoder and a SHA-256, and reports counts, timings and hashes. The decoded bytes and the
voter rows are never kept, returned or logged. Delete this file, its test and the mount line
in ``app.py`` to remove the spike.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from ev_api.auth import read_request_authorized
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_read import _d1_fetch_first, _row_value
from texas_turnout_scraper.early_vote_sync import _DAY_SELECT_HASH_SQL

CIVIX_GET_FILE_URL = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
SPIKE_USER_AGENT = "tx-turnout-ev-api-preview spike (Abstract Data)"
MIN_SECONDS_BETWEEN_CALLS = 10.0

ELECTION_CALENDARS: dict[str, tuple[date, date]] = {
    "53815": (date(2026, 10, 14), date(2026, 10, 30)),
}

_MAX_DECODED_BYTES = 128 * 1024 * 1024
_MAX_PREFIX_BYTES = 128
_SNIFF_BYTES = 512
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PREFIX_RE = re.compile(rb'\s*\{\s*"upload"\s*:\s*"')
_JSON_ESCAPE_RE = re.compile(rb"\\(?:/|u00(?:2[bBfF]|3[dD]))")
_JSON_ESCAPES = {b"\\/": b"/", b"\\u002b": b"+", b"\\u002f": b"/", b"\\u003d": b"="}
_ESCAPE_MAX_BYTES = 6
_WHITESPACE = b" \r\n\t"
_CHALLENGE_MARKERS = (b"<html", b"<!doctype html", b"just a moment", b"challenge-platform")
_TIMING_NOTE = (
    "Workers clocks advance on I/O only, so ttfb_ms and total_ms count network waits and not "
    "the CPU time spent decoding"
)

Fetch = Callable[[str, dict[str, str]], Awaitable[Any]]


class EnvelopeError(ValueError):
    """The body is not a well-formed ``{"upload":"<base64>"}`` envelope."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unescape(data: bytes) -> bytes:
    unescaped = _JSON_ESCAPE_RE.sub(lambda m: _JSON_ESCAPES[m.group(0).lower()], data)
    if b"\\" in unescaped:
        raise EnvelopeError("unsupported_escape")
    return unescaped


class EnvelopeStreamDecoder:
    """Incremental decoder for the Civix envelope. Memory is one chunk plus under 4 characters.

    A caller that needs the decoded bytes rather than only their hash sets ``sink`` to a callback
    (``spike_stage`` canonicalizes through it). The decoder itself never keeps them.
    """

    sink: Callable[[bytes], None] | None = None

    def __init__(self) -> None:
        self._sha256 = hashlib.sha256()
        self._state = "head"
        self._head = b""
        self._held = b""
        self._remainder = b""
        self.decoded_bytes = 0
        self.envelope_chars = 0
        self.chunks = 0

    @property
    def done(self) -> bool:
        return self._state == "done"

    @property
    def decoded_sha256(self) -> str:
        return self._sha256.hexdigest()

    def feed(self, chunk: bytes) -> None:
        self.chunks += 1
        if self._state == "done":
            return
        if self._state == "head":
            self._head += chunk
            match = _PREFIX_RE.match(self._head)
            if match is None:
                if len(self._head) > _MAX_PREFIX_BYTES:
                    raise EnvelopeError("bad_prefix")
                return
            chunk = self._head[match.end() :]
            self._head = b""
            self._state = "body"
        self._consume(chunk, final=False)

    def finish(self) -> None:
        if self._state == "head":
            raise EnvelopeError("bad_prefix")
        if self._state == "body":
            self._consume(b"", final=True)
        if self._state != "done":
            raise EnvelopeError("truncated")

    def _consume(self, chunk: bytes, *, final: bool) -> None:
        data = self._held + chunk
        self._held = b""
        end = data.find(b'"')
        if end != -1:
            data = data[:end]
        elif not final and b"\\" in data:
            cut = data.rfind(b"\\")
            if len(data) - cut < _ESCAPE_MAX_BYTES:
                data, self._held = data[:cut], data[cut:]
        if b"\\" in data:
            data = _unescape(data)
        data = data.translate(None, _WHITESPACE)
        self._decode(self._remainder + data, closed=end != -1)
        if end != -1:
            self._state = "done"

    def _decode(self, text: bytes, *, closed: bool) -> None:
        usable = len(text) if closed else len(text) // 4 * 4
        text, self._remainder = text[:usable], text[usable:]
        if not text:
            return
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EnvelopeError("bad_base64") from exc
        self.envelope_chars += len(text)
        self.decoded_bytes += len(raw)
        if self.decoded_bytes > _MAX_DECODED_BYTES:
            raise EnvelopeError("too_large")
        self._sha256.update(raw)
        if self.sink is not None:
            self.sink(raw)


async def body_chunks(body: Any) -> AsyncIterator[bytes]:
    """Yield a JS ``ReadableStream`` (or any async iterable of bytes) one chunk at a time."""
    get_reader = getattr(body, "getReader", None)
    if get_reader is None:
        async for piece in body:
            yield bytes(piece)
        return
    reader = get_reader()
    finished = False
    try:
        while True:
            result = await reader.read()
            if result.done:
                finished = True
                return
            if result.value is not None:
                yield result.value.to_bytes()
    finally:
        if not finished:
            with contextlib.suppress(Exception):
                await reader.cancel()


def looks_like_html_challenge(content_type: str | None, head: bytes) -> bool:
    if content_type and "text/html" in content_type.lower():
        return True
    lowered = head.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def memory_note() -> str:
    try:
        from pyodide_js import _module

        return (
            f"wasm_heap_bytes={int(_module.HEAPU8.length)} read after the stream "
            "(Python heap only, never shrinks, excludes the JS heap)"
        )
    except Exception as exc:  # noqa: BLE001 - best-effort probe of an unverified runtime API
        return f"not exposed by this runtime ({type(exc).__name__})"


def portal_url(election_id: str, ev_date: date) -> str:
    query = f"type=EVR_STATEWIDE&electionId={election_id}&electionDate={ev_date:%m/%d/%Y}"
    return f"{CIVIX_GET_FILE_URL}?{query}"


def _selected_headers(headers: Any) -> dict[str, Any]:
    def get(name: str) -> str | None:
        value = headers.get(name)
        return None if value is None else str(value)

    return {
        "content-type": get("content-type"),
        "content-length": get("content-length"),
        "server": get("server"),
        "cf-mitigated": get("cf-mitigated"),
        "cf-ray-present": get("cf-ray") is not None,
        "retry-after": get("retry-after"),
    }


def _elapsed_ms(clock: Callable[[], float], since: float) -> int:
    return int((clock() - since) * 1000)


async def probe_portal(
    fetch: Fetch,
    url: str,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, object]:
    """One request, streamed and decoded. Returns counts, timings and a hash only."""
    started = clock()
    decoder = EnvelopeStreamDecoder()
    report: dict[str, object] = {
        "http_status": None,
        "response_headers": None,
        "ttfb_ms": None,
        "body_looks_like_html_challenge": False,
        "error": None,
    }
    complete = False
    try:
        response = await fetch(url, {"User-Agent": SPIKE_USER_AGENT, "Accept": "application/json"})
        status = int(response.status)
        selected = _selected_headers(response.headers)
        report.update(
            {
                "http_status": status,
                "response_headers": selected,
                "ttfb_ms": _elapsed_ms(clock, started),
            }
        )
        challenge = looks_like_html_challenge(selected["content-type"], b"")
        head = bytearray()
        if response.body is not None:
            async with contextlib.aclosing(body_chunks(response.body)) as pieces:
                async for piece in pieces:
                    if len(head) < _SNIFF_BYTES:
                        head += piece[: _SNIFF_BYTES - len(head)]
                        challenge = looks_like_html_challenge(selected["content-type"], bytes(head))
                    if status != 200 or challenge:
                        break
                    decoder.feed(piece)
                    if decoder.done:
                        break
        report["body_looks_like_html_challenge"] = challenge
        if status == 200 and not challenge:
            decoder.finish()
            complete = True
    except EnvelopeError as exc:
        report["error"] = {"class": type(exc).__name__, "reason": exc.reason}
    except Exception as exc:  # noqa: BLE001 - report the class only, never the message
        report["error"] = {"class": type(exc).__name__}
    report.update(
        {
            "total_ms": _elapsed_ms(clock, started),
            "envelope_chars": decoder.envelope_chars,
            "decoded_bytes": decoder.decoded_bytes,
            "decoded_sha256": decoder.decoded_sha256 if complete else None,
            "chunks": decoder.chunks,
            "peak_memory_note": memory_note(),
            "timing_note": _TIMING_NOTE,
        }
    )
    return report


async def stored_day_hash(db: Any, election_id: str, ev_date: str) -> str | None:
    row = await _d1_fetch_first(db.prepare(_DAY_SELECT_HASH_SQL).bind(election_id, ev_date))
    if row is None:
        return None
    digest = _row_value(row, "sha256")
    return digest if isinstance(digest, str) and digest else None


class _CallGate:
    """One call per ``MIN_SECONDS_BETWEEN_CALLS`` in this isolate."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._last: float | None = None

    def claim(self) -> int | None:
        now = self._clock()
        if self._last is not None and now - self._last < MIN_SECONDS_BETWEEN_CALLS:
            return max(1, int(MIN_SECONDS_BETWEEN_CALLS - (now - self._last)) + 1)
        self._last = now
        return None


async def _workers_fetch(url: str, headers: dict[str, str]) -> Any:
    from workers import fetch

    return await fetch(url, method="GET", headers=headers)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=400, headers={"Cache-Control": "no-store"})


def register_spike_routes(
    app: FastAPI,
    deps_for_request: Callable[[Request], Any],
    *,
    fetch: Fetch | None = None,
    clock: Callable[[], float] = time.monotonic,
    today: Callable[[], date] = _utc_today,
) -> None:
    gate = _CallGate(clock)

    async def portal_fetch(
        request: Request,
        election_id: str | None = Query(default=None),
        ev_date: str | None = Query(default=None, alias="date"),
    ) -> Response:
        active = deps_for_request(request)
        if not await read_request_authorized(
            request.headers,
            access_client_id=active.access_client_id,
            access_client_secret=active.access_client_secret,
            kv=active.kv,
            token_db=active.token_db,
        ):
            return Response(status_code=401)
        try:
            normalized_id = require_election_id(election_id)
        except ValueError:
            return _bad_request("bad_request")
        if ev_date is None or _DATE_RE.fullmatch(ev_date) is None:
            return _bad_request("bad_request")
        try:
            day = date.fromisoformat(ev_date)
        except ValueError:
            return _bad_request("bad_request")
        window = ELECTION_CALENDARS.get(normalized_id)
        if window is None:
            return _bad_request("election_not_allowed")
        if day > today():
            return _bad_request("date_in_future")
        if not window[0] <= day <= window[1]:
            return _bad_request("date_outside_calendar")
        retry_after = gate.claim()
        if retry_after is not None:
            return JSONResponse(
                {"detail": "rate_limited"},
                status_code=429,
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        report = await probe_portal(fetch or _workers_fetch, portal_url(normalized_id, day))
        report.update({"election_id": normalized_id, "ev_date": ev_date})
        report.update(await _stored_comparison(active, normalized_id, ev_date, report))
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    app.add_api_route("/spike/portal-fetch", portal_fetch, methods=["GET"], response_model=None)


async def _stored_comparison(
    active: Any, election_id: str, ev_date: str, report: dict[str, object]
) -> dict[str, object]:
    out: dict[str, object] = {
        "stored_sha256": None,
        "stored_matches_raw_decoded": None,
        "stored_hash_is_over_canonical_bytes": True,
    }
    db = active.rosters.database(election_id)
    if db is None:
        out["stored_lookup"] = "no_binding"
        return out
    try:
        stored = await stored_day_hash(db, election_id, ev_date)
    except Exception as exc:  # noqa: BLE001 - report the class only
        out["stored_lookup"] = f"failed:{type(exc).__name__}"
        return out
    out["stored_sha256"] = stored
    decoded = report["decoded_sha256"]
    if stored is not None and decoded is not None:
        out["stored_matches_raw_decoded"] = stored == decoded
    return out

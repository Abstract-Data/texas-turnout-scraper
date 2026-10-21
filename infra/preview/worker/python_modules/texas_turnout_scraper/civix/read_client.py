"""Swappable client for the early-vote FastAPI read API.

The Worker caches each JSON response under ``ev:<election>:v<N>:<resource>``
(for example ``ev:49664:v42:county:453``) after reading ``ev:<election>:version``.
The ETag is that version. This client sends ``If-None-Match`` and treats HTTP
304 as not modified. D1 is queried only on a cache miss. Old versions are not
purged; they expire by TTL.

KV can take about a minute to show a new version at every edge. That window
is acceptable for this daily file. This client does not offer a faster path.

``HttpxReadTransport`` speaks HTTP. A later Cloudflare service binding named
``EV_API`` implements :class:`ReadTransport` and leaves :meth:`EarlyVoteReadClient.get`
unchanged.

httpx treats 304 as a failure: ``Response.is_success`` is only 2xx, and
``raise_for_status`` raises ``HTTPStatusError`` for 304
(https://www.python-httpx.org/exceptions/). This module checks the status
code and never calls ``raise_for_status``, so a not-modified response is not
turned into an exception and the response body never enters the error text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from .ids import require_election_id
from .models import ElectionId
from .transport import join_base_url

EV_API_BINDING = "EV_API"
HTTP_OK = 200
HTTP_NOT_MODIFIED = 304
PURGE_OLD_VERSIONS = False

CF_ACCESS_CLIENT_ID_HEADER = "CF-Access-Client-Id"
CF_ACCESS_CLIENT_SECRET_HEADER = "CF-Access-Client-Secret"

_SEGMENT_FORBIDDEN = frozenset("/:?#\\")


class ReadClientError(Exception):
    """Read API failure whose message is path, status, and a fixed reason.

    The message never includes the response body or Access credentials.
    """

    def __init__(self, *, path: str, status_code: int, reason: str) -> None:
        self.path = path
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"read API {reason} for {path} (HTTP {status_code})")


@dataclass(frozen=True)
class TransportResponse:
    """One GET result. Header names are matched case-insensitively by the client."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class ReadTransport(Protocol):
    """Call shape shared by httpx and a future ``EV_API`` service binding."""

    async def send(self, path: str, *, headers: Mapping[str, str]) -> TransportResponse:
        """GET ``path`` with ``headers`` and return the status, headers, and body."""


class ReadResult(BaseModel):
    """One read. ``election_id`` is stored as a string. ``body`` is unset on 304."""

    model_config = ConfigDict(frozen=True)

    election_id: ElectionId
    segments: tuple[str, ...]
    status_code: int
    etag: str | None
    not_modified: bool
    body: dict[str, Any] | None


def version_key(election_id: object) -> str:
    """KV key that holds the current version number for one election."""
    return f"ev:{require_election_id(election_id)}:version"


def cache_key(election_id: object, version: object, *segments: object) -> str:
    """Versioned KV key. Old keys are not purged and are not read again."""
    election = require_election_id(election_id)
    version_no = _require_version(version)
    suffix = ":".join(_require_segments(segments))
    return f"ev:{election}:v{version_no}:{suffix}"


def single_flight_lock_key(election_id: object, version: object, *segments: object) -> str:
    """KV cold-miss lock for one aggregate endpoint at one published version.

    Example: ``ev:53815:v7:lock:aggregates:county-by-day``
    """
    election = require_election_id(election_id)
    version_no = _require_version(version)
    endpoint = ":".join(_require_segments(segments))
    return f"ev:{election}:v{version_no}:lock:{endpoint}"


def http_path(election_id: object, *segments: object) -> str:
    """HTTP path for the same resource ``cache_key`` addresses.

    ``("county", "453")`` is ``/elections/<id>/county/453``. The version stays
    in the ETag, not in the path. The Worker reads ``version_key`` itself.
    """
    election = require_election_id(election_id)
    tail = "/".join(_require_segments(segments))
    return f"/elections/{election}/{tail}"


def etag_for_version(version: object) -> str:
    """Strong ETag tied to the KV version, for example ``\"v42\"``."""
    return f'"v{_require_version(version)}"'


def version_from_etag(etag: str) -> int:
    """Return the version number encoded in ``etag``."""
    inner = _quoted_etag(etag)[1:-1]
    if len(inner) < 2 or not inner.startswith("v") or not inner[1:].isdigit():
        msg = "etag is not tied to a version"
        raise ValueError(msg)
    version = int(inner[1:])
    if version < 1:
        msg = "etag is not tied to a version"
        raise ValueError(msg)
    return version


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` requires HTTP 304 for this version ETag.

    ``*`` matches. A comma-separated list matches if any validator matches.
    Comparison ignores a weak ``W/`` prefix. ``None`` and ``""`` do not match.
    """
    if if_none_match is None:
        return False
    candidate = if_none_match.strip()
    if not candidate:
        return False
    if candidate == "*":
        return True
    expected = _quoted_etag(etag)
    return any(_quoted_etag(part) == expected for part in candidate.split(","))


def queries_d1(*, cache_hit: bool) -> bool:
    """D1 is read only when the versioned KV entry is missing."""
    if not isinstance(cache_hit, bool):
        msg = "cache_hit must be bool"
        raise TypeError(msg)
    return not cache_hit


class EarlyVoteReadClient:
    """Read early-vote JSON through an injected :class:`ReadTransport`."""

    def __init__(self, transport: ReadTransport) -> None:
        self._transport = transport

    async def get(
        self,
        election_id: object,
        *segments: object,
        if_none_match: str | None = None,
    ) -> ReadResult:
        """GET one resource. A 304 leaves ``body`` unset and does not parse it."""
        if if_none_match is not None and not isinstance(if_none_match, str):
            msg = "if_none_match must be a string"
            raise TypeError(msg)
        path = http_path(election_id, *segments)
        headers: dict[str, str] = {}
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        response = await self._transport.send(path, headers=headers)
        etag = _header(response.headers, "etag")
        if response.status_code == HTTP_NOT_MODIFIED:
            return ReadResult(
                election_id=require_election_id(election_id),
                segments=_require_segments(segments),
                status_code=HTTP_NOT_MODIFIED,
                etag=etag,
                not_modified=True,
                body=None,
            )
        if response.status_code != HTTP_OK:
            raise ReadClientError(
                path=path, status_code=response.status_code, reason="request failed"
            )
        return ReadResult(
            election_id=require_election_id(election_id),
            segments=_require_segments(segments),
            status_code=HTTP_OK,
            etag=etag,
            not_modified=False,
            body=_json_object(response.body, path),
        )


class HttpxReadTransport:
    """httpx implementation of :class:`ReadTransport`.

    ``client`` is optional. When it is omitted, each call opens its own
    ``httpx.AsyncClient``. Pass ``base_url`` either way: the request URL is
    absolute, so a stand-in client does not have to join ``base_url`` itself.
    Per-request ``timeout`` overrides the client timeout
    (https://www.python-httpx.org/advanced/timeouts/).
    """

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        access_client_id: str | None = None,
        access_client_secret: str | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            msg = "base_url is required"
            raise ValueError(msg)
        self._base_url = base_url
        self._client = client
        self._timeout = timeout
        self._access_headers = _access_headers(access_client_id, access_client_secret)

    async def send(self, path: str, *, headers: Mapping[str, str]) -> TransportResponse:
        """GET ``path``. 304 is returned as a response, not raised."""
        url = join_base_url(self._base_url, path)
        merged = {**headers, **self._access_headers}
        if self._client is not None:
            response = await self._client.get(url, headers=merged, timeout=self._timeout)
            return _from_httpx(response)
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            response = await owned.get(url, headers=merged, timeout=self._timeout)
            return _from_httpx(response)


def _access_headers(
    access_client_id: str | None,
    access_client_secret: str | None,
) -> dict[str, str]:
    if (access_client_id is None) != (access_client_secret is None):
        msg = "Access service token needs both client id and secret"
        raise ValueError(msg)
    if access_client_id is None or access_client_secret is None:
        return {}
    if access_client_id == "" or access_client_secret == "":
        msg = "Access service token needs both client id and secret"
        raise ValueError(msg)
    return {
        CF_ACCESS_CLIENT_ID_HEADER: access_client_id,
        CF_ACCESS_CLIENT_SECRET_HEADER: access_client_secret,
    }


def _from_httpx(response: httpx.Response) -> TransportResponse:
    headers = dict(response.headers.items())
    return TransportResponse(
        status_code=response.status_code, headers=headers, body=response.content
    )


def _require_version(version: object) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        msg = "version must be a positive int"
        raise ValueError(msg)
    return version


def _require_segment(segment: object) -> str:
    if not isinstance(segment, str) or segment == "" or segment != segment.strip():
        msg = "resource segment must be a non-empty string"
        raise ValueError(msg)
    if segment in {".", ".."} or any(char in _SEGMENT_FORBIDDEN for char in segment):
        msg = "resource segment must not contain a separator"
        raise ValueError(msg)
    return segment


def _require_segments(segments: tuple[object, ...]) -> tuple[str, ...]:
    if not segments:
        msg = "resource path is required"
        raise ValueError(msg)
    return tuple(_require_segment(segment) for segment in segments)


def _quoted_etag(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("w/"):
        text = text[2:].strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        msg = "etag must be a quoted string"
        raise ValueError(msg)
    return text


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value
    return None


def _json_object(body: bytes, path: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReadClientError(
            path=path,
            status_code=HTTP_OK,
            reason="response was not a JSON object",
        ) from None
    if not isinstance(parsed, dict):
        raise ReadClientError(
            path=path,
            status_code=HTTP_OK,
            reason="response was not a JSON object",
        )
    return parsed

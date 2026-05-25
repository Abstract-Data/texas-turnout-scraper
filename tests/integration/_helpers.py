"""Helpers for live integration tests (test package only)."""

from __future__ import annotations

import httpx
import pytest
from requests.exceptions import HTTPError as RequestsHTTPError
from requests.exceptions import RequestException

LIVE_HTTP_ERRORS = (
    httpx.HTTPStatusError,
    httpx.RequestError,
    RequestsHTTPError,
    RequestException,
)


def http_status_code(exc: BaseException) -> int | None:
    """Extract HTTP status from httpx or requests errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, RequestsHTTPError) and exc.response is not None:
        return exc.response.status_code
    return None


def is_retryable_http_error(exc: BaseException, codes: set[int]) -> bool:
    """True when *exc* is an HTTP error with a status in *codes*."""
    status = http_status_code(exc)
    return status is not None and status in codes


def skip_on_live_http_error(exc: BaseException, *, context: str) -> None:
    """Skip when the live endpoint is unavailable; re-raise unexpected errors."""
    status = http_status_code(exc)
    if status in {403, 404, 429, 500, 502, 503, 504}:
        pytest.skip(f"{context}: HTTP {status} from live API")
    raise exc

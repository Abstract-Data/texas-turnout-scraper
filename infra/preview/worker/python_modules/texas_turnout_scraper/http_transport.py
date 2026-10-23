"""HTTP transport with optional Cloudflare/WAF bypass via cloudscraper.

Unit tests keep ``backend="httpx"`` so respx can mock requests. Production and
live integration tests default to ``backend="cloudscraper"``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

import httpx
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

try:
    from requests.exceptions import HTTPError as _RequestsHTTPError
    from requests.exceptions import RequestException as _RequestsRequestException
except ImportError:
    # requests is installed with the waf extra (cloudscraper), not the core package.
    _REQUESTS_EXCEPTIONS: tuple[type[BaseException], ...] = ()
else:
    _REQUESTS_EXCEPTIONS = (_RequestsHTTPError, _RequestsRequestException)

logger = logging.getLogger(__name__)

HttpBackend = Literal["httpx", "cloudscraper"]

# Exceptions raised by either httpx (tests) or cloudscraper/requests (production).
HTTP_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    *_REQUESTS_EXCEPTIONS,
)

# Civix ENR/archive JSON retry. Tenacity's ``initial`` argument is deprecated
# and forwarded to ``multiplier``; pass ``multiplier`` directly.
JSON_RETRY_ATTEMPTS = 3
JSON_RETRY_MULTIPLIER_SECONDS = 0.25
JSON_RETRY_MAX_SECONDS = 2.0
JSON_RETRY_JITTER_SECONDS = 0.2

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_MAX_HTTP_RETRIES = 2

# Caps for untrusted remote payloads. A base64 envelope is ~4/3 the size of the
# 512 MiB decoded cap, so the response cap sits above that.
MAX_RESPONSE_BYTES = 1024 * 1024 * 1024
MAX_DECODED_PAYLOAD_BYTES = 512 * 1024 * 1024


class PayloadTooLargeError(ValueError):
    """A remote payload exceeded a configured size cap. Carries no content."""


def check_response_size(response: Any, limit: int = MAX_RESPONSE_BYTES) -> None:
    """Raise :class:`PayloadTooLargeError` when a buffered response exceeds ``limit``."""
    headers = getattr(response, "headers", None)
    declared = None
    if headers is not None:
        try:
            declared = int(headers.get("content-length", ""))
        except (TypeError, ValueError, AttributeError):
            declared = None
    content = getattr(response, "content", None)
    actual = len(content) if isinstance(content, (bytes, bytearray)) else 0
    if max(declared or 0, actual) > limit:
        msg = f"Response exceeded the {limit}-byte size cap"
        raise PayloadTooLargeError(msg)


def format_fetch_error(exc: BaseException) -> str:
    """Short error label for logs (no voter PII)."""
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            return f"HTTP {status_code}"
    return type(exc).__name__


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Abstract-Data/texas-turnout-scraper (Texas SOS public EV data; "
        "+https://github.com/Abstract-Data/texas-turnout-scraper)"
    ),
}


def _validate_backend(backend: str) -> HttpBackend:
    if backend not in ("httpx", "cloudscraper"):
        msg = f"http_backend must be 'httpx' or 'cloudscraper', got {backend!r}"
        raise ValueError(msg)
    return backend  # type: ignore[return-value]


class PacedHttpClient:
    """GET/POST client backed by httpx or cloudscraper (requests session)."""

    def __init__(
        self,
        base_url: str,
        *,
        backend: HttpBackend = "cloudscraper",
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._backend = _validate_backend(backend)
        self._follow_redirects = follow_redirects
        merged_headers = {**_DEFAULT_HEADERS, **(headers or {})}

        if self._backend == "cloudscraper":
            try:
                import cloudscraper
            except ImportError as exc:
                msg = (
                    "http_backend='cloudscraper' requires the waf extra "
                    "(pip install 'texas-turnout-scraper[waf]')"
                )
                raise ImportError(msg) from exc

            self._scraper = cloudscraper.create_scraper()
            self._scraper.headers.update(merged_headers)
            self._httpx: httpx.Client | None = None
        else:
            self._scraper = None
            self._httpx = httpx.Client(
                base_url=base_url,
                timeout=timeout,
                headers=merged_headers,
                follow_redirects=follow_redirects,
            )

    @property
    def backend(self) -> HttpBackend:
        return self._backend

    @property
    def cookies(self) -> Any:
        """Cookie jar (httpx or requests), for session-based portals."""
        if self._httpx is not None:
            return self._httpx.cookies
        return self._scraper.cookies

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}{path}"

    def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        retry_status_codes: frozenset[int] = _RETRYABLE_STATUS_CODES,
        max_retries: int = _MAX_HTTP_RETRIES,
        **kwargs: Any,
    ) -> Any:
        last_response: Any = None
        for attempt in range(max_retries + 1):
            if self._httpx is not None:
                if method.upper() == "GET":
                    last_response = self._httpx.get(path, **kwargs)
                else:
                    last_response = self._httpx.request(method, path, data=data, **kwargs)
            else:
                last_response = self._scraper.request(
                    method,
                    self._url(path),
                    data=data,
                    timeout=self._timeout,
                    allow_redirects=self._follow_redirects,
                    **kwargs,
                )
            status_code = getattr(last_response, "status_code", None)
            if status_code in retry_status_codes and attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(0, 0.5))
                continue
            last_response.raise_for_status()
            check_response_size(last_response)
            return last_response
        if last_response is not None:
            last_response.raise_for_status()
        msg = f"{method.upper()} request failed without a response"
        raise RuntimeError(msg)

    def get(
        self,
        path: str,
        *,
        retry_status_codes: frozenset[int] = _RETRYABLE_STATUS_CODES,
        max_retries: int = _MAX_HTTP_RETRIES,
        **kwargs: Any,
    ) -> Any:
        return self._request_with_retry(
            "GET",
            path,
            retry_status_codes=retry_status_codes,
            max_retries=max_retries,
            **kwargs,
        )

    def post(
        self,
        path: str,
        data: dict[str, str] | None = None,
        *,
        retry_status_codes: frozenset[int] = _RETRYABLE_STATUS_CODES,
        max_retries: int = _MAX_HTTP_RETRIES,
        **kwargs: Any,
    ) -> Any:
        return self._request_with_retry(
            "POST",
            path,
            data=data,
            retry_status_codes=retry_status_codes,
            max_retries=max_retries,
            **kwargs,
        )

    @contextmanager
    def stream(self, method: str, path: str, **kwargs: Any) -> Iterator[Any]:
        """Stream a request body (httpx native; requests via ``stream=True``)."""
        if self._httpx is not None:
            with self._httpx.stream(method, path, **kwargs) as response:
                yield response
            return

        response = self._scraper.request(
            method,
            self._url(path),
            timeout=self._timeout,
            allow_redirects=self._follow_redirects,
            stream=True,
            **kwargs,
        )
        try:
            yield _RequestsStreamAdapter(response)
        finally:
            response.close()

    def close(self) -> None:
        if self._httpx is not None:
            self._httpx.close()
        elif self._scraper is not None:
            self._scraper.close()


class _RequestsStreamAdapter:
    """Minimal adapter so Strategy B can use ``iter_bytes`` on requests responses."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    def iter_bytes(self, chunk_size: int = 65536) -> Iterator[bytes]:
        for chunk in self._response.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk


def is_retryable_json_status(status_code: int) -> bool:
    """True for HTTP statuses the Civix JSON clients treat as transient."""
    return status_code == 429 or 500 <= status_code <= 599


def is_transient_json_http_error(exc: BaseException) -> bool:
    """True when an httpx error from a Civix JSON call is safe to retry."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_json_status(exc.response.status_code)
    return False


def json_retry_wait() -> wait_exponential_jitter:
    """Exponential backoff with jitter for Civix JSON clients.

    Wait is ``multiplier * 2**n + random(0, jitter)``, capped at ``max``.
    """
    return wait_exponential_jitter(
        multiplier=JSON_RETRY_MULTIPLIER_SECONDS,
        max=JSON_RETRY_MAX_SECONDS,
        jitter=JSON_RETRY_JITTER_SECONDS,
    )


async def _json_retry_sleep(seconds: float) -> None:
    """Sleep used by the JSON retry policy. Tests replace this."""
    await asyncio.sleep(seconds)


async def _run_before_attempt(before_attempt: Callable[[], Awaitable[None] | None] | None) -> None:
    if before_attempt is None:
        return
    result = before_attempt()
    if isinstance(result, Awaitable):
        await result


async def get_json_with_retry(
    *,
    client: Any,
    url: str,
    timeout: float,
    headers: dict[str, str],
    retry_predicate: Callable[[BaseException], bool] | None = None,
    before_attempt: Callable[[], Awaitable[None] | None] | None = None,
    on_response: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """GET JSON with a bounded tenacity retry. Non-object JSON becomes ``{}``."""
    predicate = retry_predicate or is_transient_json_http_error
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(JSON_RETRY_ATTEMPTS),
        wait=json_retry_wait(),
        retry=retry_if_exception(predicate),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        sleep=_json_retry_sleep,
        reraise=True,
    ):
        with attempt:
            await _run_before_attempt(before_attempt)
            response = await client.get(url, timeout=timeout, headers=headers)
            if on_response is not None:
                on_response(response)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
    msg = "Unreachable: AsyncRetrying should return or raise"
    raise RuntimeError(msg)

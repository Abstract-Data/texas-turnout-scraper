"""HTTP transport with optional Cloudflare/WAF bypass via cloudscraper.

Unit tests keep ``backend="httpx"`` so respx can mock requests. Production and
live integration tests default to ``backend="cloudscraper"``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import httpx
from requests.exceptions import HTTPError as RequestsHTTPError
from requests.exceptions import RequestException as RequestsRequestException

HttpBackend = Literal["httpx", "cloudscraper"]

# Exceptions raised by either httpx (tests) or cloudscraper/requests (production).
HTTP_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    RequestsHTTPError,
    RequestsRequestException,
)

_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
_MAX_HTTP_RETRIES = 2


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
        "Mozilla/5.0 (compatible; texas-turnout-scraper/1.0; "
        "+https://github.com/abstract-data/texas-turnout-scraper)"
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
            import cloudscraper

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

    def get(
        self,
        path: str,
        *,
        retry_status_codes: frozenset[int] = _RETRYABLE_STATUS_CODES,
        max_retries: int = _MAX_HTTP_RETRIES,
        **kwargs: Any,
    ) -> Any:
        last_response: Any = None
        for attempt in range(max_retries + 1):
            if self._httpx is not None:
                last_response = self._httpx.get(path, **kwargs)
            else:
                last_response = self._scraper.get(
                    self._url(path),
                    timeout=self._timeout,
                    allow_redirects=self._follow_redirects,
                    **kwargs,
                )
            status_code = getattr(last_response, "status_code", None)
            if (
                status_code in retry_status_codes
                and attempt < max_retries
            ):
                import time

                time.sleep(2.0 * (attempt + 1))
                continue
            last_response.raise_for_status()
            return last_response
        if last_response is not None:
            last_response.raise_for_status()
        msg = "GET request failed without a response"
        raise RuntimeError(msg)

    def post(
        self, path: str, data: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        if self._httpx is not None:
            return self._httpx.post(path, data=data, **kwargs)
        return self._scraper.post(
            self._url(path),
            data=data,
            timeout=self._timeout,
            allow_redirects=self._follow_redirects,
            **kwargs,
        )

    @contextmanager
    def stream(
        self, method: str, path: str, **kwargs: Any
    ) -> Iterator[Any]:
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

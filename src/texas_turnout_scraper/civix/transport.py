"""Shared JSON HTTP helpers for Civix ENR and archive clients.

``base_url`` is injected by the caller. Absolute URLs are built here so a
stand-in client (tests, a later service binding) does not have to implement
httpx's ``base_url`` joining. Owned clients are still constructed with
``httpx.AsyncClient(base_url=...)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..http_transport import get_json_with_retry

DEFAULT_ENR_BASE_URL = "https://goelect.txelections.civixapps.com/api-ivis-system/api"
DEFAULT_ARCHIVE_BASE_URL = "https://results.texas-election.com"
DEFAULT_JSON_TIMEOUT_SECONDS = 20
DEFAULT_JSON_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def join_base_url(base_url: str, path: str) -> str:
    """Join ``base_url`` and ``path``. Absolute ``path`` values pass through."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = base_url.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{suffix}"


def json_request_headers(user_agent: str = DEFAULT_JSON_USER_AGENT) -> dict[str, str]:
    """Headers that reduce Civix WAF false-positives for API traffic."""
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": user_agent,
    }


async def fetch_json(
    path: str,
    *,
    base_url: str,
    client: Any | None = None,
    timeout: float = DEFAULT_JSON_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
    before_attempt: Callable[[], Awaitable[None] | None] | None = None,
    on_response: Callable[[Any], None] | None = None,
    retry_predicate: Callable[[BaseException], bool] | None = None,
) -> dict[str, Any]:
    """GET JSON from ``base_url`` + ``path`` with the shared retry policy."""
    url = join_base_url(base_url, path)
    request_headers = headers or json_request_headers()
    request_timeout = max(timeout, 1)
    if client is not None:
        return await get_json_with_retry(
            client=client,
            url=url,
            timeout=request_timeout,
            headers=request_headers,
            before_attempt=before_attempt,
            on_response=on_response,
            retry_predicate=retry_predicate,
        )
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=request_headers,
        timeout=request_timeout,
    ) as owned:
        return await get_json_with_retry(
            client=owned,
            url=url,
            timeout=request_timeout,
            headers=request_headers,
            before_attempt=before_attempt,
            on_response=on_response,
            retry_predicate=retry_predicate,
        )

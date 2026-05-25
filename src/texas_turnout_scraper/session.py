"""Session management for the legacy Texas SOS early-voting portal.

The SOS portal at ``earlyvoting.texas-election.com`` is a stateful Java/Struts
application. A valid ``JSESSIONID`` cookie must be obtained before any data
requests are made. This module provides :class:`LegacySession`, a context manager
that establishes the session and wraps rate-paced HTTP operations.

HTTP flow:
    1. GET  ``/Elections/getElectionDetails.do``          → sets JSESSIONID
    2. POST ``/Elections/getElectionEVDates.do``           → HTML with EV dates
    3. POST ``/Elections/getEVDetails.do``                 → county report page
    4. POST ``/Elections/downloadVoterInfoReport.do``      → per-county CSV roster
"""

from __future__ import annotations

import time
from typing import Any

from .http_transport import HttpBackend, PacedHttpClient
from .models import LegacyEVDate


class LegacySession:
    """Context manager that wraps an HTTP session for the legacy SOS portal.

    Establishes a ``JSESSIONID`` cookie on entry and exposes rate-paced
    ``GET``/``POST`` helpers used by the higher-level scraping modules.

    Args:
        pace_seconds: Minimum seconds to wait between consecutive requests.
            Defaults to 1.0 to match the legacy ingest convention.
        timeout: Total request timeout in seconds. Defaults to 30.0.
        http_backend: ``"cloudscraper"`` (default) bypasses WAF; use ``"httpx"`` in unit tests.

    Example::

        with LegacySession(pace_seconds=1.0) as sess:
            resp = sess._post_form("/Elections/getElectionEVDates.do",
                                   {"idElection": "49664"})
    """

    BASE_URL = "https://earlyvoting.texas-election.com"
    DEFAULT_PACE = 1.0

    def __init__(
        self,
        pace_seconds: float = DEFAULT_PACE,
        timeout: float = 30.0,
        http_backend: HttpBackend = "cloudscraper",
    ) -> None:
        self._pace_seconds = pace_seconds
        self._timeout = timeout
        self._last_request_at: float = 0.0
        self._client = PacedHttpClient(
            self.BASE_URL,
            backend=http_backend,
            timeout=timeout,
            follow_redirects=True,
        )
        self._primed_election_id: str | None = None
        self._cached_ev_dates: list[LegacyEVDate] | None = None
        self._election_details_html: str | None = None

    # ------------------------------------------------------------------
    # Session establishment
    # ------------------------------------------------------------------

    def prime_election(self, source_election_id: str) -> list[LegacyEVDate]:
        """Step 2: POST ``getElectionEVDates.do`` (required before turnout/roster).

        Must be called after :meth:`establish` and before ``getEVDetails.do`` or
        per-county roster downloads. Wraps :func:`~texas_turnout_scraper.elections.get_ev_dates`.

        Args:
            source_election_id: SOS election ID string (e.g. ``"49664"``). Never int.

        Returns:
            Available early-voting dates for the election.
        """
        if (
            self._primed_election_id == source_election_id
            and self._cached_ev_dates is not None
        ):
            return self._cached_ev_dates

        from .elections import get_ev_dates

        ev_dates = get_ev_dates(self, source_election_id)
        self._primed_election_id = source_election_id
        self._cached_ev_dates = ev_dates
        return ev_dates

    def establish(self) -> None:
        """Step 1: Hit the main election page to obtain a JSESSIONID cookie.

        The portal sets the session cookie in response to the very first GET
        request. All subsequent POSTs reuse the same client cookie jar.

        Raises:
            HTTP error if the portal returns a non-2xx status.
        """
        self._primed_election_id = None
        self._cached_ev_dates = None
        self._election_details_html = None

        self._pace()
        resp = self._client.get("/Elections/getElectionDetails.do")
        resp.raise_for_status()
        self._election_details_html = resp.text
        self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pace(self) -> None:
        """Block until at least ``pace_seconds`` have elapsed since the last request."""
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._pace_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _post_form(self, path: str, data: dict[str, str]) -> Any:
        """Submit a rate-paced ``application/x-www-form-urlencoded`` POST.

        Args:
            path: URL path relative to :attr:`BASE_URL`, e.g.
                ``"/Elections/getElectionEVDates.do"``.
            data: Form fields to encode in the request body.

        Returns:
            HTTP response (httpx or requests).

        Raises:
            HTTP error if the server returns a non-2xx status.
        """
        self._pace()
        resp = self._client.post(path, data=data)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp

    def get(self, path: str, **kwargs: object) -> Any:
        """Rate-paced GET request.

        Args:
            path: URL path relative to :attr:`BASE_URL`.
            **kwargs: Extra keyword arguments forwarded to the HTTP client.

        Returns:
            HTTP response (httpx or requests).
        """
        self._pace()
        resp = self._client.get(path, **kwargs)  # type: ignore[arg-type]
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._client.close()

    def __enter__(self) -> LegacySession:
        self.establish()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

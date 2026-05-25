"""Civix EVR API client for the Texas SOS new portal.

Endpoint base: https://goelect.txelections.civixapps.com

This client is FULLY STATELESS — no session establishment, no cookies,
plain authenticated GET requests only. Every endpoint returns a universal
``{"upload": "<base64>"}`` envelope which is decoded before parsing.

Pacing: all requests are rate-limited to ≥ ``pace_seconds`` between calls
(default 1.0 s) to match the legacy ingest convention and avoid hammering
the SOS infrastructure.

PII note: ``ID_VOTER`` (Texas VUID) and ``VOTER_NAME`` values are NEVER
logged anywhere in this module.
"""

from __future__ import annotations

import base64
import csv
import io
import time
import zipfile
from datetime import date
from typing import Any

from .enums import VoteMethod
from .http_transport import HttpBackend, PacedHttpClient
from .models import (
    CivixCountyRef,
    CivixCountyTurnout,
    CivixElection,
    CivixElectionDate,
    CountyRoster,
    VoterRecord,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://goelect.txelections.civixapps.com"
API_PREFIX = "/api-ivis-system/api/v1"
DEFAULT_PACE_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Envelope helper
# ---------------------------------------------------------------------------


def _decode_envelope(response: Any) -> bytes:
    """Decode the universal Civix base64 envelope.

    Every Civix EVR endpoint returns a JSON body of the form::

        {"upload": "<base64-encoded payload>"}

    This helper extracts and decodes that payload to raw bytes, which may
    be UTF-8 text (JSON or CSV) or binary (ZIP) depending on the endpoint.

    Civix sometimes returns HTTP 200 with an **empty body** for per-county
    rosters that have no voters on that date (``roster_available`` may still
    be true in the turnout table). Treat that as an empty payload.

    Args:
        response: HTTP response from Civix (httpx or requests, via ``.json()``).

    Returns:
        The decoded payload as raw :class:`bytes`.

    Raises:
        KeyError: If the response JSON lacks the ``"upload"`` key.
        ValueError: If the response body is non-empty but not valid JSON.
        httpx.HTTPStatusError: Propagated from the caller if status != 2xx.
    """
    body = (response.text or "").strip()
    if not body:
        return b""
    try:
        envelope = response.json()
    except ValueError as exc:
        msg = "Civix response was not valid JSON"
        raise ValueError(msg) from exc
    upload = envelope.get("upload")
    if upload is None:
        raise KeyError("upload")
    return base64.b64decode(upload)


# ---------------------------------------------------------------------------
# CivixClient
# ---------------------------------------------------------------------------


class CivixClient:
    """Stateless HTTP client for the Civix EVR API.

    All requests are plain GET calls — no session cookies, no POST bodies.
    Pacing is enforced at the ``_get`` level so all public methods benefit
    automatically.

    Usage::

        with CivixClient() as client:
            elections = client.list_elections()

    Args:
        pace_seconds: Minimum seconds between consecutive requests (default 1.0).
        timeout: Request timeout in seconds (default 30.0).
        http_backend: ``"cloudscraper"`` (default) bypasses WAF; use ``"httpx"`` in unit tests.
    """

    def __init__(
        self,
        pace_seconds: float = DEFAULT_PACE_SECONDS,
        timeout: float = 30.0,
        http_backend: HttpBackend = "cloudscraper",
    ) -> None:
        self._client = PacedHttpClient(
            BASE_URL,
            backend=http_backend,
            timeout=timeout,
        )
        self._pace = pace_seconds
        self._last_request: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        """Rate-paced GET request.

        Enforces ≥ ``pace_seconds`` between consecutive outbound requests
        using ``time.monotonic()``. Raises on non-2xx responses.

        Args:
            path: URL path relative to ``BASE_URL`` (must start with ``/``).
            params: Optional query parameters dict.

        Returns:
            HTTP response (httpx or requests, depending on ``http_backend``).
        """
        elapsed = time.monotonic() - self._last_request
        wait = self._pace - elapsed
        if wait > 0:
            time.sleep(wait)

        response = self._client.get(path, params=params)
        self._last_request = time.monotonic()
        return response

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> CivixClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def list_elections(self) -> list[CivixElection]:
        """Fetch the full election index from the Civix EVR API.

        Calls::

            GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTION

        Returns:
            List of :class:`~texas_turnout_scraper.models.CivixElection`
            objects, one per election in the index.
        """
        path = f"{API_PREFIX}/getFile"
        response = self._get(path, params={"type": "EVR_ELECTION"})
        payload = _decode_envelope(response)
        data = payload  # raw bytes — parse as JSON text
        import json as _json

        elections_data = _json.loads(data.decode("utf-8"))

        results: list[CivixElection] = []
        for e in elections_data.get("elections", []):
            ev_dates = [
                CivixElectionDate(
                    date=d["date"],
                    date_turnout_id=d["date_turnout_id"],
                )
                for d in e.get("early_voting_dates", [])
            ]
            counties = [
                CivixCountyRef(
                    county_id=c["county_id"],
                    name=c["name"],
                )
                for c in e.get("counties", [])
            ]
            election = CivixElection(
                source_election_id=str(e["id"]),
                id=e["id"],
                type=e.get("type", "EV"),
                election_date=e["election_date"],
                election_name=e["election_name"],
                certified=bool(e.get("certified", False)),
                early_voting_dates=ev_dates,
                counties=counties,
            )
            results.append(election)

        return results

    def fetch_ev_turnout(
        self,
        election_id: int,
        election_date: date,
    ) -> list[CivixCountyTurnout]:
        """Fetch early-voting turnout summary by county for one election date.

        Calls::

            GET /api-ivis-system/api/v1/getFile
                ?type=EVR_EARLYVOTING
                &electionId={election_id}
                &electionDate={MM/DD/YYYY}

        Args:
            election_id: Civix integer election ID.
            election_date: The EV date to retrieve turnout for.

        Returns:
            List of :class:`~texas_turnout_scraper.models.CivixCountyTurnout`,
            one per county.
        """
        import json as _json

        date_str = election_date.strftime("%m/%d/%Y")
        path = f"{API_PREFIX}/getFile"
        response = self._get(
            path,
            params={
                "type": "EVR_EARLYVOTING",
                "electionId": election_id,
                "electionDate": date_str,
            },
        )
        payload = _decode_envelope(response)
        data = _json.loads(payload.decode("utf-8"))

        results: list[CivixCountyTurnout] = []
        for county in data.get("turnout_by_county", []):
            vdr = county.get("voter_details_report")
            roster_available = isinstance(vdr, str) or vdr is True
            results.append(
                CivixCountyTurnout(
                    election_id=str(election_id),
                    report_date=election_date,
                    county=county["name"],
                    county_id=county["id"],
                    registered_voters=int(county.get("registered_voters", 0)),
                    in_person_votes_on_date=int(county.get("in_person_votes_on_date", 0)),
                    total_in_person_votes=int(county.get("total_in_person_votes_for_election", 0)),
                    total_mail_votes=int(county.get("total_mail_votes_for_election", 0)),
                    roster_available=roster_available,
                    source="civix",
                )
            )

        return results

    def fetch_ev_roster_csv(
        self,
        election_id: int,
        election_date: date,
        county_name: str,
        county_id: int,
    ) -> list[VoterRecord]:
        """Fetch the early-voting voter roster CSV for one county and date.

        Calls::

            GET /api-ivis-system/api/v1/getFileByFormat
                ?type=EVR_EARLYVOTING
                &electionId={election_id}
                &electionDate={MM/DD/YYYY}
                &county={county_name}
                &countyId={county_id}
                &format=csv

        PII note: ``ID_VOTER`` and ``VOTER_NAME`` values are NEVER logged.

        Args:
            election_id: Civix integer election ID.
            election_date: The EV date to retrieve roster for.
            county_name: All-caps county name (e.g. ``"TRAVIS"``).
            county_id: Civix integer county ID.

        Returns:
            List of :class:`~texas_turnout_scraper.models.VoterRecord`.
        """
        date_str = election_date.strftime("%m/%d/%Y")
        path = f"{API_PREFIX}/getFileByFormat"
        response = self._get(
            path,
            params={
                "type": "EVR_EARLYVOTING",
                "electionId": election_id,
                "electionDate": date_str,
                "county": county_name,
                "countyId": county_id,
                "format": "csv",
            },
        )
        csv_bytes = _decode_envelope(response)
        if not csv_bytes.strip():
            return []

        csv_text = csv_bytes.decode("utf-8")
        records: list[VoterRecord] = []
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            records.append(
                VoterRecord(
                    id_voter=str(row["ID_VOTER"]).zfill(10),  # always string — never int
                    voting_method=VoteMethod(row["VOTING_METHOD"]),
                    precinct=row["PRECINCT"],
                    county=county_name,
                    election_id=str(election_id),
                    report_date=election_date,
                    voter_name=row.get("VOTER_NAME", ""),  # stored for mismatch detection only
                )
            )

        return records

    def fetch_statewide(
        self,
        election_id: int,
        election_date: date,
    ) -> bytes:
        """Fetch the statewide early-voting roster file.

        Calls::

            GET /api-ivis-system/api/v1/getFile
                ?type=EVR_STATEWIDE
                &electionId={election_id}
                &electionDate={MM/DD/YYYY}

        Note:
            This endpoint returns HTTP 502 for large elections. The decoded
            payload may be a flat CSV or a ZIP archive depending on election
            size. Callers are responsible for detecting the format from the
            leading bytes (``b"PK"`` = ZIP).

        Args:
            election_id: Civix integer election ID.
            election_date: The EV date to retrieve statewide data for.

        Returns:
            Raw decoded bytes — either CSV text or a ZIP archive.
        """
        date_str = election_date.strftime("%m/%d/%Y")
        path = f"{API_PREFIX}/getFile"
        response = self._get(
            path,
            params={
                "type": "EVR_STATEWIDE",
                "electionId": election_id,
                "electionDate": date_str,
            },
        )
        return _decode_envelope(response)

    def fetch_polling_places(
        self,
        election_id: int,
        name: str = "STATEWIDE_POLLING_PLACE_INFO",
    ) -> str:
        """Fetch polling place information as a CSV text string.

        Calls::

            GET /api-ivis-system/api/v1/getFileByFormat
                ?type=EVR_COUNTYPLACEINFO
                &electionId={election_id}
                &name={name}
                &format=csv

        Args:
            election_id: Civix integer election ID.
            name: Polling place report name (default ``"STATEWIDE_POLLING_PLACE_INFO"``).

        Returns:
            Decoded CSV text as a plain :class:`str`.
        """
        path = f"{API_PREFIX}/getFileByFormat"
        response = self._get(
            path,
            params={
                "type": "EVR_COUNTYPLACEINFO",
                "electionId": election_id,
                "name": name,
                "format": "csv",
            },
        )
        raw = _decode_envelope(response)
        return raw.decode("utf-8")

    def fetch_ed_turnout(
        self,
        election_id: int,
        election_date: date,
    ) -> list[CivixCountyTurnout]:
        """Fetch election-day turnout summary by county for one date.

        Calls::

            GET /api-ivis-system/api/v1/getFile
                ?type=EVR_ELECTIONDAYTURNOUT
                &electionId={election_id}
                &electionDate={MM/DD/YYYY}

        Identical structure to :meth:`fetch_ev_turnout` but uses the
        election-day turnout endpoint.

        Args:
            election_id: Civix integer election ID.
            election_date: The election date to retrieve turnout for.

        Returns:
            List of :class:`~texas_turnout_scraper.models.CivixCountyTurnout`,
            one per county.
        """
        import json as _json

        date_str = election_date.strftime("%m/%d/%Y")
        path = f"{API_PREFIX}/getFile"
        response = self._get(
            path,
            params={
                "type": "EVR_ELECTIONDAYTURNOUT",
                "electionId": election_id,
                "electionDate": date_str,
            },
        )
        payload = _decode_envelope(response)
        data = _json.loads(payload.decode("utf-8"))

        results: list[CivixCountyTurnout] = []
        for county in data.get("turnout_by_county", []):
            vdr = county.get("voter_details_report")
            roster_available = isinstance(vdr, str) or vdr is True
            results.append(
                CivixCountyTurnout(
                    election_id=str(election_id),
                    report_date=election_date,
                    county=county["name"],
                    county_id=county["id"],
                    registered_voters=int(county.get("registered_voters", 0)),
                    in_person_votes_on_date=int(county.get("in_person_votes_on_date", 0)),
                    total_in_person_votes=int(county.get("total_in_person_votes_for_election", 0)),
                    total_mail_votes=int(county.get("total_mail_votes_for_election", 0)),
                    roster_available=roster_available,
                    source="civix",
                )
            )

        return results

    def fetch_ed_roster_zip(
        self,
        election_id: int,
        election_date: date,
        county_name: str,
        county_id: int,
    ) -> list[VoterRecord]:
        """Fetch the election-day voter roster ZIP for one county and date.

        Calls::

            GET /api-ivis-system/api/v1/getFileByFormat
                ?type=EVR_ELECTIONDAYTURNOUT
                &electionId={election_id}
                &electionDate={MM/DD/YYYY}
                &county={county_name}
                &countyId={county_id}
                &format=zip

        The decoded payload is a ZIP archive containing one or more CSV files.
        All CSV files inside are parsed and concatenated into a single list.

        PII note: ``ID_VOTER`` and ``VOTER_NAME`` values are NEVER logged.

        Args:
            election_id: Civix integer election ID.
            election_date: The election date to retrieve roster for.
            county_name: All-caps county name (e.g. ``"TRAVIS"``).
            county_id: Civix integer county ID.

        Returns:
            Concatenated list of :class:`~texas_turnout_scraper.models.VoterRecord`
            from all CSV files inside the ZIP.
        """
        date_str = election_date.strftime("%m/%d/%Y")
        path = f"{API_PREFIX}/getFileByFormat"
        response = self._get(
            path,
            params={
                "type": "EVR_ELECTIONDAYTURNOUT",
                "electionId": election_id,
                "electionDate": date_str,
                "county": county_name,
                "countyId": county_id,
                "format": "zip",
            },
        )
        zip_bytes = _decode_envelope(response)

        records: list[VoterRecord] = []
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for member in zf.namelist():
                if not member.lower().endswith(".csv"):
                    continue
                with zf.open(member) as csv_file:
                    csv_text = csv_file.read().decode("utf-8")
                    reader = csv.DictReader(io.StringIO(csv_text))
                    for row in reader:
                        records.append(
                            VoterRecord(
                                id_voter=str(row["ID_VOTER"]).zfill(
                                    10
                                ),  # always string — never int
                                voting_method=VoteMethod(row["VOTING_METHOD"]),
                                precinct=row["PRECINCT"],
                                county=county_name,
                                election_id=str(election_id),
                                report_date=election_date,
                                voter_name=row.get(
                                    "VOTER_NAME", ""
                                ),  # stored for mismatch detection only
                            )
                        )

        return records


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def fetch_county_roster(
    client: CivixClient,
    election_id: int,
    election_date: date,
    county_name: str,
    county_id: int,
    is_election_day: bool = False,
) -> CountyRoster:
    """Fetch roster for one county and return as a :class:`CountyRoster`.

    Dispatches to :meth:`CivixClient.fetch_ed_roster_zip` when
    ``is_election_day=True``, otherwise uses :meth:`CivixClient.fetch_ev_roster_csv`.

    Args:
        client: An open :class:`CivixClient` instance.
        election_id: Civix integer election ID.
        election_date: The date to retrieve the roster for.
        county_name: All-caps county name (e.g. ``"TRAVIS"``).
        county_id: Civix integer county ID.
        is_election_day: If ``True``, use the election-day ZIP endpoint;
            otherwise use the early-voting CSV endpoint (default ``False``).

    Returns:
        A :class:`~texas_turnout_scraper.models.CountyRoster` populated with
        all voter records for the requested county and date.
    """
    if is_election_day:
        records = client.fetch_ed_roster_zip(
            election_id=election_id,
            election_date=election_date,
            county_name=county_name,
            county_id=county_id,
        )
    else:
        records = client.fetch_ev_roster_csv(
            election_id=election_id,
            election_date=election_date,
            county_name=county_name,
            county_id=county_id,
        )

    return CountyRoster(
        county=county_name,
        county_id=county_id,
        election_id=str(election_id),
        report_date=election_date,
        source="civix",
        records=records,
    )

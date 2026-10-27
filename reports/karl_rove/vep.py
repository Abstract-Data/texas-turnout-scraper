"""VEP match-fast HTTP client (Bearer auth, browser User-Agent)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from reports.karl_rove.join import vep_years_from_api
from reports.karl_rove.models import VepYearsSummary
from reports.karl_rove.ndjson import parse_ndjson_line
from reports.karl_rove.vep_index import VepMatchIndex

logger = logging.getLogger(__name__)

DEFAULT_VEP_BASE_URLS = (
    "https://data.txvoterengagement.com",
    "https://vep-match-api.johnreakin.workers.dev",
)

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NDJSON_PATH = "/v1/matches.ndjson"


class VepClientError(Exception):
    """VEP API failure; message is HTTP status only (no response body)."""

    def __init__(self, *, path: str, status_code: int) -> None:
        self.path = path
        self.status_code = status_code
        super().__init__(f"VEP request failed path={path} status={status_code}")


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key.strip():
        msg = "VEP_API_KEY is required"
        raise ValueError(msg)
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": "application/x-ndjson, application/json",
    }


def resolve_vep_base_url(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    return DEFAULT_VEP_BASE_URLS[0]


def _get_json(
    client: httpx.Client,
    path: str,
    *,
    params: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    response = client.get(path, params=params)
    if response.status_code != httpx.codes.OK:
        raise VepClientError(path=path, status_code=response.status_code)
    parsed = response.json()
    if not isinstance(parsed, dict):
        raise VepClientError(path=path, status_code=response.status_code)
    return parsed


def fetch_vep_years(client: httpx.Client) -> VepYearsSummary:
    payload = _get_json(client, "/v1/years")
    return vep_years_from_api(payload)


def _next_cursor(headers: httpx.Headers) -> str | None:
    for key in ("x-next-cursor", "X-Next-Cursor", "next-cursor"):
        value = headers.get(key)
        if value:
            return str(value).strip()
    return None


def stream_vep_year_ndjson(client: httpx.Client, vep_cohort_year: int) -> VepMatchIndex:
    """Stream ``matches.ndjson`` for one VEP cohort year into an in-memory index."""
    index = VepMatchIndex()
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {
            "year": vep_cohort_year,
            "fields": "core,voter",
            "limit": 10_000,
        }
        if cursor:
            params["cursor"] = cursor
        with client.stream("GET", NDJSON_PATH, params=params, timeout=300.0) as response:
            if response.status_code != httpx.codes.OK:
                raise VepClientError(path=NDJSON_PATH, status_code=response.status_code)
            for line in response.iter_lines():
                if not line:
                    continue
                row = parse_ndjson_line(line)
                if row is not None:
                    index.ingest(row)
            next_cursor = _next_cursor(response.headers)
        pages += 1
        if not next_cursor:
            break
        cursor = next_cursor
    logger.info("VEP ndjson pages fetched year=%s pages=%s", vep_cohort_year, pages)
    return index


def load_vep_indexes(client: httpx.Client, vep_years: VepYearsSummary) -> VepMatchIndex:
    combined = VepMatchIndex()
    for year_row in vep_years.years:
        chunk = stream_vep_year_ndjson(client, year_row.vep_cohort_year)
        for cohort_year, vuids in chunk.cohort_vuids.items():
            combined.cohort_vuids[cohort_year].update(vuids)
        for reg_year, vuids in chunk.registration_year_vuids.items():
            combined.registration_year_vuids[reg_year].update(vuids)
        logger.info(
            "VEP cohort indexed year=%s registrants=%s",
            year_row.vep_cohort_year,
            len(chunk.cohort_vuids.get(year_row.vep_cohort_year, set())),
        )
    return combined


def open_vep_client(api_key: str, base_url: str | None) -> httpx.Client:
    primary = resolve_vep_base_url(base_url)
    headers = _auth_headers(api_key)
    return httpx.Client(base_url=primary, headers=headers, timeout=120.0, follow_redirects=True)


def open_vep_client_with_fallback(api_key: str, base_url: str | None) -> httpx.Client:
    """Try explicit or primary URL; on connection errors try the fallback host."""
    explicit = base_url.strip() if base_url else None
    if explicit:
        return open_vep_client(api_key, explicit)
    primary = DEFAULT_VEP_BASE_URLS[0]
    fallback = DEFAULT_VEP_BASE_URLS[1]
    headers = _auth_headers(api_key)
    try:
        probe = httpx.Client(base_url=primary, headers=headers, timeout=30.0)
        response = probe.get("/v1/years")
        if response.status_code == httpx.codes.OK:
            probe.close()
            return httpx.Client(base_url=primary, headers=headers, timeout=120.0)
        logger.warning("VEP primary host returned HTTP %s; trying fallback", response.status_code)
        probe.close()
    except httpx.HTTPError:
        logger.warning("VEP primary host unreachable; trying fallback")
    return httpx.Client(base_url=fallback, headers=headers, timeout=120.0)

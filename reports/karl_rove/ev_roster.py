"""Early-vote read API: aggregates for as-of date, roster CSV for VUID set (in memory)."""

from __future__ import annotations

import csv
import io
import logging

import httpx

from reports.karl_rove.vuid import normalize_vuid
from texas_turnout_scraper.civix.read_client import (
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_CLIENT_SECRET_HEADER,
    HttpxReadTransport,
    join_base_url,
)

logger = logging.getLogger(__name__)

GENERAL_2026_ELECTION_ID = "53815"
GENERAL_2026_LABEL = "2026 General Election"


class EvReadError(Exception):
    """Early-vote API failure without credentials or row data in the message."""


def _ev_headers(
    *,
    access_client_id: str | None,
    access_client_secret: str | None,
    bearer_token: str,
) -> dict[str, str]:
    headers: dict[str, str] = {"Authorization": f"Bearer {bearer_token.strip()}"}
    if access_client_id and access_client_secret:
        headers[CF_ACCESS_CLIENT_ID_HEADER] = access_client_id
        headers[CF_ACCESS_CLIENT_SECRET_HEADER] = access_client_secret
    return headers


async def fetch_latest_ev_date(
    *,
    base_url: str,
    access_client_id: str | None,
    access_client_secret: str | None,
    bearer_token: str,
    source_election_id: str,
) -> str:
    """Requires aggregates-scoped consumer token (or a token that can read aggregates)."""
    from texas_turnout_scraper.civix.read_client import EarlyVoteReadClient

    transport = HttpxReadTransport(
        base_url=base_url.rstrip("/"),
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
        bearer_token=bearer_token,
        timeout=60.0,
    )
    client = EarlyVoteReadClient(transport)
    result = await client.get(source_election_id, "aggregates", "by-day")
    body = result.body
    if body is None:
        raise EvReadError("aggregates/by-day returned no body")
    series = body.get("series")
    if not isinstance(series, list) or not series:
        raise EvReadError("aggregates/by-day series empty")
    dates: list[str] = []
    for item in series:
        if isinstance(item, dict) and isinstance(item.get("ev_date"), str):
            dates.append(item["ev_date"])
    if not dates:
        raise EvReadError("no ev_date in by-day series")
    return max(dates)


def _parse_roster_vuids(csv_bytes: bytes) -> frozenset[str]:
    text = csv_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return frozenset()
    key = "ID_VOTER" if "ID_VOTER" in reader.fieldnames else "id_voter"
    seen: set[str] = set()
    for row in reader:
        raw = row.get(key)
        if raw:
            seen.add(normalize_vuid(str(raw)))
    return frozenset(seen)


async def fetch_early_voted_vuids(
    *,
    base_url: str,
    access_client_id: str | None,
    access_client_secret: str | None,
    bearer_token: str,
    source_election_id: str,
    ev_date: str,
) -> frozenset[str]:
    """Stream latest-day roster CSV; distinct VUIDs kept only in memory.

    Uses roster-scoped consumer token (no batch VUID lookup on the read API).
    """
    url = join_base_url(base_url, f"/elections/{source_election_id}/roster/{ev_date}")
    headers = _ev_headers(
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
        bearer_token=bearer_token,
    )
    chunks: list[bytes] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code != httpx.codes.OK:
                raise EvReadError(
                    f"roster GET HTTP {response.status_code} election={source_election_id}"
                )
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
    data = b"".join(chunks)
    vuids = _parse_roster_vuids(data)
    logger.info(
        "early-vote roster parsed election=%s ev_date=%s distinct_voters=%s",
        source_election_id,
        ev_date,
        len(vuids),
    )
    return vuids

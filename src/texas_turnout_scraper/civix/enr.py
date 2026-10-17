"""Civix ENR API client.

Election ids on the returned models are strings. Pass either a digit string
or a positive ``int``; the stored value is a string.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .discover import (
    election_id_sort_key,
    election_ref_from_row,
    iter_election_info_rows,
    normalized_type_codes,
)
from .envelope import (
    decode_county_info_payload,
    decode_election_constants,
    decode_election_payload,
)
from .ids import require_election_id
from .models import CivixCountyInfoPayload, CivixElectionPayload, CivixElectionRef
from .transport import DEFAULT_ENR_BASE_URL, DEFAULT_JSON_TIMEOUT_SECONDS, fetch_json

# Civix display timestamps are UTC. Attaching Central time shifts them by the
# offset when a later step converts "CT" to UTC.
CENTRAL_TZ = ZoneInfo("America/Chicago")

_UPDATED_TIME_FORMATS = (
    "%b %d, %Y %H:%M:%S",
    "%B %d, %Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)


def parse_civix_updated_time(value: str | None) -> datetime | None:
    """Parse Civix ``LastUpdatedTime`` display strings into UTC datetimes."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    for fmt in _UPDATED_TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def discover_election_refs(
    election_constants: dict[str, Any],
    *,
    year: str | None = None,
    type_codes: set[str] | None = None,
    max_items: int | None = None,
) -> list[CivixElectionRef]:
    """Extract election refs from decoded electionConstants."""
    refs = _collect_enr_refs(election_constants, year=year, type_codes=type_codes)
    refs.sort(key=election_id_sort_key, reverse=True)
    if max_items is not None and max_items > 0:
        return refs[:max_items]
    return refs


def _collect_enr_refs(
    election_constants: dict[str, Any],
    *,
    year: str | None,
    type_codes: set[str] | None,
) -> list[CivixElectionRef]:
    desired_year = year.strip() if year else ""
    allowed_types = normalized_type_codes(type_codes)
    refs: list[CivixElectionRef] = []
    for year_value, type_value, election_key, election_data in iter_election_info_rows(
        election_constants
    ):
        if desired_year and year_value != desired_year:
            continue
        if allowed_types and type_value not in allowed_types:
            continue
        ref = election_ref_from_row(
            year_value=year_value,
            type_value=type_value,
            election_key=election_key,
            election_data=election_data,
            id_from_key=False,
        )
        if ref is not None:
            refs.append(ref)
    return refs


def json_payload_hash(payload: Any) -> str:
    """Stable SHA-256 hash for JSON-compatible payloads."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _fetch_json(
    path: str,
    *,
    client: Any | None = None,
    base_url: str = DEFAULT_ENR_BASE_URL,
    timeout: float = DEFAULT_JSON_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Request JSON from the ENR API. ``base_url`` overrides the Civix host."""
    return await fetch_json(path, base_url=base_url, client=client, timeout=timeout)


async def fetch_election_constants(
    *,
    client: httpx.AsyncClient | None = None,
    base_url: str = DEFAULT_ENR_BASE_URL,
) -> dict[str, Any]:
    """Fetch and decode the election constants payload."""
    raw_payload = await _fetch_json("/s3/enr/electionConstants", client=client, base_url=base_url)
    return decode_election_constants(raw_payload)


async def fetch_election_payload(
    election_id: str | int,
    *,
    client: httpx.AsyncClient | None = None,
    base_url: str = DEFAULT_ENR_BASE_URL,
) -> CivixElectionPayload:
    """Fetch and decode the election payload for one election id."""
    election_key = require_election_id(election_id)
    raw_payload = await _fetch_json(
        f"/s3/enr/election/{election_key}",
        client=client,
        base_url=base_url,
    )
    decoded_sections = decode_election_payload(raw_payload)
    source_updated_raw, source_updated_at = _source_updated(decoded_sections)
    return CivixElectionPayload(
        election_id=election_key,
        fetched_at=datetime.now(CENTRAL_TZ),
        source_updated_at=source_updated_at,
        source_updated_raw=source_updated_raw,
        raw_payload=raw_payload,
        decoded_sections=decoded_sections,
    )


async def fetch_county_info_payload(
    election_id: str | int,
    *,
    client: httpx.AsyncClient | None = None,
    base_url: str = DEFAULT_ENR_BASE_URL,
) -> CivixCountyInfoPayload:
    """Fetch and decode the countyInfo payload for one election id."""
    election_key = require_election_id(election_id)
    raw_payload = await _fetch_json(
        f"/s3/enr/election/countyInfo/{election_key}",
        client=client,
        base_url=base_url,
    )
    return CivixCountyInfoPayload(
        election_id=election_key,
        fetched_at=datetime.now(CENTRAL_TZ),
        raw_payload=raw_payload,
        counties_by_id=decode_county_info_payload(raw_payload),
    )


def _source_updated(decoded_sections: dict[str, Any]) -> tuple[str, datetime | None]:
    home = decoded_sections.get("Home")
    if not isinstance(home, dict):
        return "", None
    source_updated_raw = str(home.get("LastUpdatedTime") or "").strip()
    return source_updated_raw, parse_civix_updated_time(source_updated_raw)


# Re-export decode helpers that QR tests and callers import from the client.
__all__ = [
    "CENTRAL_TZ",
    "CivixCountyInfoPayload",
    "CivixElectionPayload",
    "CivixElectionRef",
    "decode_county_info_payload",
    "decode_election_constants",
    "decode_election_payload",
    "discover_election_refs",
    "fetch_county_info_payload",
    "fetch_election_constants",
    "fetch_election_payload",
    "json_payload_hash",
    "parse_civix_updated_time",
]

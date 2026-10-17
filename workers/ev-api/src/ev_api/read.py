"""Versioned KV cache in front of one D1 database per election.

Key, path, and ETag encoding come from
``texas_turnout_scraper.civix.read_client``. This module does not
reimplement them.

Order, locked by Decision 4A:

1. Read ``version_key``.
2. If ``etag_matches`` is true, return 304. D1 is not queried.
3. Otherwise read ``cache_key``.
4. On a miss, ``queries_d1`` is true: query that election's D1 once and
   store the body. Old version keys are left in place.

``ttl_seconds`` is unset. The read-client contract says old versions expire
by TTL and does not choose a number of seconds. This function does not
invent one. When ``ttl_seconds`` is ``None``, the KV put omits
``expirationTtl``.

~60s KV propagation is not fit for election-night results. A get() without
``cacheTtl`` still uses the 60-second default edge cache. This module does
not pass a shorter ``cacheTtl`` and does not add another store.

https://developers.cloudflare.com/kv/api/write-key-value-pairs/
https://developers.cloudflare.com/kv/api/read-key-value-pairs/#cachettl-parameter
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.civix.read_client import (
    cache_key,
    etag_for_version,
    etag_matches,
    http_path,
    queries_d1,
    version_key,
)

logger = logging.getLogger(__name__)


class KvNamespace(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None: ...


class ElectionStore(Protocol):
    async def fetch_body(self, source_election_id: str, resource: str) -> str | None: ...


@dataclass(frozen=True)
class CountyQuery:
    source_election_id: str
    county_id: str
    if_none_match: str | None


@dataclass(frozen=True)
class ReadOutcome:
    status_code: int
    etag: str | None
    body: dict[str, object] | None
    cache: str


def _object_json(raw: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _version_number(raw: str | None) -> int | None:
    """KV stores the version as decimal digits. The contract wants a positive int."""
    if raw is None or not raw.isdigit():
        return None
    version = int(raw)
    if version < 1:
        return None
    return version


def _resource(key: str, version: int) -> str:
    """Suffix of the contract cache key, for example ``county:453``."""
    marker = f":v{version}:"
    return key.split(marker, 1)[1]


async def read_county(
    query: CountyQuery,
    kv: KvNamespace,
    store: ElectionStore,
    *,
    ttl_seconds: int | None = None,
) -> ReadOutcome:
    election_id = require_election_id(query.source_election_id)
    http_path(election_id, "county", query.county_id)
    version_raw = await kv.get(version_key(election_id))
    version = _version_number(version_raw)
    if version is None:
        if version_raw is not None:
            logger.warning("invalid version token election=%s", election_id)
        return ReadOutcome(404, None, None, "absent")

    etag = etag_for_version(version)
    try:
        matched = etag_matches(query.if_none_match, etag)
    except ValueError:
        return ReadOutcome(400, etag, None, "bad_etag")
    if matched:
        return ReadOutcome(304, etag, None, "not_modified")

    key = cache_key(election_id, version, "county", query.county_id)
    cached = await kv.get(key)
    cached_body = _object_json(cached) if cached is not None else None
    if not queries_d1(cache_hit=cached_body is not None):
        return ReadOutcome(200, etag, cached_body, "hit")
    if cached is not None:
        logger.warning("cached payload was not an object election=%s", election_id)

    raw = await store.fetch_body(election_id, _resource(key, version))
    if raw is None:
        return ReadOutcome(404, etag, None, "miss")
    body = _object_json(raw)
    if body is None:
        logger.warning("d1 payload was not an object election=%s", election_id)
        return ReadOutcome(502, etag, None, "miss")
    await kv.put(key, raw, expiration_ttl=ttl_seconds)
    return ReadOutcome(200, etag, body, "miss")


def response_headers(outcome: ReadOutcome) -> dict[str, str]:
    if outcome.etag is None:
        return {}
    return {"ETag": outcome.etag, "Cache-Control": "private"}

"""Versioned KV cache in front of per-election D1 aggregate reads."""

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
    queries_d1,
    version_key,
)
from texas_turnout_scraper.early_vote_sync import hash_key

logger = logging.getLogger(__name__)

# Daily early-vote snapshots: old version keys expire by TTL; they are not purged.
DEFAULT_KV_TTL_SECONDS = 86_400


class KvNamespace(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None: ...


class AggregateStore(Protocol):
    async def build_body(
        self,
        source_election_id: str,
        segments: tuple[str, ...],
        *,
        kv_sha256: str,
    ) -> dict[str, object] | None: ...


@dataclass(frozen=True)
class ResourceQuery:
    source_election_id: str
    segments: tuple[str, ...]
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
    if raw is None or not raw.isdigit():
        return None
    version = int(raw)
    if version < 1:
        return None
    return version


async def read_resource(
    query: ResourceQuery,
    kv: KvNamespace,
    store: AggregateStore,
    *,
    ttl_seconds: int | None = DEFAULT_KV_TTL_SECONDS,
) -> ReadOutcome:
    election_id = require_election_id(query.source_election_id)
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

    key = cache_key(election_id, version, *query.segments)
    cached = await kv.get(key)
    cached_body = _object_json(cached) if cached is not None else None
    if not queries_d1(cache_hit=cached_body is not None):
        return ReadOutcome(200, etag, cached_body, "hit")
    if cached is not None:
        logger.warning("cached payload was not an object election=%s", election_id)

    kv_sha256 = await kv.get(hash_key(election_id))
    if kv_sha256 is None or kv_sha256 == "":
        return ReadOutcome(404, etag, None, "miss")

    body = await store.build_body(
        election_id,
        query.segments,
        kv_sha256=kv_sha256,
    )
    if body is None:
        return ReadOutcome(404, etag, None, "miss")
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    await kv.put(key, raw, expiration_ttl=ttl_seconds)
    return ReadOutcome(200, etag, body, "miss")


def response_headers(outcome: ReadOutcome) -> dict[str, str]:
    if outcome.etag is None:
        return {}
    return {"ETag": outcome.etag, "Cache-Control": "private"}

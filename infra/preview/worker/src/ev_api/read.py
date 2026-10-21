"""Versioned KV cache in front of per-election D1 aggregate reads.

Cold-cache stampede guard: a best-effort KV lock per election/version/endpoint
(``single_flight_lock_key``) with a 60s TTL. KV ``get``/``put`` are not atomic,
so concurrent misses may still overlap; the lock narrows the race. Holders that
see an existing lock recheck the versioned cache once, then return 503 with
``Retry-After`` instead of starting another D1 chunk walk.
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
    queries_d1,
    single_flight_lock_key,
    version_key,
)
from texas_turnout_scraper.early_vote_sync import hash_key

logger = logging.getLogger(__name__)

# Daily early-vote snapshots: old version keys expire by TTL; they are not purged.
DEFAULT_KV_TTL_SECONDS = 86_400

# Cloudflare KV minimum expiration TTL for single-flight locks (seconds).
SINGLE_FLIGHT_LOCK_TTL_SECONDS = 60

# Suggested client backoff when another request is populating the cache.
SINGLE_FLIGHT_RETRY_AFTER_SECONDS = 5

_AGGREGATE_IN_PROGRESS_DETAIL = "aggregate_in_progress"


class KvNamespace(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None: ...

    async def delete(self, key: str) -> None: ...


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
    retry_after: int | None = None


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


def _contended_outcome(etag: str) -> ReadOutcome:
    return ReadOutcome(
        503,
        etag,
        {"detail": _AGGREGATE_IN_PROGRESS_DETAIL},
        "contended",
        retry_after=SINGLE_FLIGHT_RETRY_AFTER_SECONDS,
    )


async def _cached_hit(
    kv: KvNamespace,
    key: str,
    etag: str,
) -> ReadOutcome | None:
    cached = await kv.get(key)
    if cached is None:
        return None
    cached_body = _object_json(cached)
    if cached_body is None:
        logger.warning("cached payload was not an object key_suffix=%s", key.split(":")[-1])
        return None
    return ReadOutcome(200, etag, cached_body, "hit")


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

    lock_key = single_flight_lock_key(election_id, version, *query.segments)
    if await kv.get(lock_key) is not None:
        recheck = await _cached_hit(kv, key, etag)
        if recheck is not None:
            return recheck
        return _contended_outcome(etag)

    await kv.put(lock_key, "1", expiration_ttl=SINGLE_FLIGHT_LOCK_TTL_SECONDS)
    try:
        recheck = await _cached_hit(kv, key, etag)
        if recheck is not None:
            return recheck

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
    finally:
        await kv.delete(lock_key)


def response_headers(outcome: ReadOutcome) -> dict[str, str]:
    headers: dict[str, str] = {}
    if outcome.etag is not None:
        headers["ETag"] = outcome.etag
        headers["Cache-Control"] = "private"
    if outcome.retry_after is not None:
        headers["Retry-After"] = str(outcome.retry_after)
    return headers

"""Adapters from aggregate read protocols to Workers KV and D1 bindings."""

from __future__ import annotations

import json
import logging
from typing import Any

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_aggregate import build_aggregate_resource

logger = logging.getLogger(__name__)


class WorkersKvNamespace:
    def __init__(self, namespace: Any) -> None:
        self._namespace = namespace

    async def get(self, key: str) -> str | None:
        value = await self._namespace.get(key)
        if value is None:
            return None
        return str(value)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        if expiration_ttl is None:
            await self._namespace.put(key, value)
            return
        await self._namespace.put(key, value, {"expirationTtl": expiration_ttl})


def binding_map_from_env(raw: object) -> dict[str, str]:
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        logger.warning("EV_D1_BINDINGS is not a string")
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("EV_D1_BINDINGS is not valid JSON")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("EV_D1_BINDINGS is not an object")
        return {}
    mapping: dict[str, str] = {}
    for key, value in parsed.items():
        try:
            election_id = require_election_id(key)
        except ValueError:
            logger.warning("skipped a non-string election id in EV_D1_BINDINGS")
            continue
        if not isinstance(value, str) or value == "":
            logger.warning("skipped an empty D1 binding election=%s", election_id)
            continue
        mapping[election_id] = value
    return mapping


def _database(env: Any, binding_name: str) -> Any | None:
    database = getattr(env, binding_name, None)
    if database is None:
        return None
    return database


class WorkersAggregateStore:
    """Build aggregate JSON from chunked D1 roster / turnout publishes."""

    def __init__(self, env: Any, binding_map: dict[str, str]) -> None:
        self._env = env
        self._binding_map = binding_map

    def _db(self, source_election_id: str) -> Any | None:
        election_id = require_election_id(source_election_id)
        binding_name = self._binding_map.get(election_id)
        if binding_name is None:
            logger.info("no D1 binding for election=%s", election_id)
            return None
        return _database(self._env, binding_name)

    async def build_body(
        self,
        source_election_id: str,
        segments: tuple[str, ...],
        *,
        kv_sha256: str,
    ) -> dict[str, object] | None:
        if len(segments) < 2 or segments[0] != "aggregates":
            return None
        election_id = require_election_id(source_election_id)
        db = self._db(election_id)
        if db is None:
            return None

        resource = segments[1]
        ev_date = segments[2] if len(segments) > 2 else None
        return await build_aggregate_resource(
            db,
            election_id,
            resource,
            kv_sha256=kv_sha256,
            ev_date=ev_date,
        )

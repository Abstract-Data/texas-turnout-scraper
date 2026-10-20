"""Adapters from aggregate read protocols to Workers KV and D1 bindings."""

from __future__ import annotations

import json
import logging
from typing import Any

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_aggregates import (
    aggregates_from_decoded,
    build_by_day_series,
    build_county_by_day_series,
    slice_by_method,
)
from texas_turnout_scraper.early_vote_d1_read import (
    D1Database,
    load_day_rows,
    load_published_bytes,
    resolve_file_id_for_day,
)

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


def _database(env: Any, binding_name: str) -> D1Database | None:
    database = getattr(env, binding_name, None)
    if database is None:
        return None
    return database


class WorkersAggregateStore:
    """Build aggregate JSON from chunked D1 roster / turnout publishes."""

    def __init__(self, env: Any, binding_map: dict[str, str]) -> None:
        self._env = env
        self._binding_map = binding_map

    def _db(self, source_election_id: str) -> D1Database | None:
        election_id = require_election_id(source_election_id)
        binding_name = self._binding_map.get(election_id)
        if binding_name is None:
            logger.info("no D1 binding for election=%s", election_id)
            return None
        return _database(self._env, binding_name)

    async def _day_body(
        self,
        db: D1Database,
        election_id: str,
        *,
        kv_sha256: str,
        ev_date: str,
        file_id: str | None = None,
    ) -> dict[str, object] | None:
        resolved = file_id
        if resolved is None:
            resolved = await resolve_file_id_for_day(db, election_id, ev_date)
        decoded = await load_published_bytes(
            db,
            election_id,
            kv_sha256=kv_sha256,
            file_id=resolved,
        )
        if decoded is None:
            return None
        return aggregates_from_decoded(
            decoded,
            source_election_id=election_id,
            ev_date=ev_date,
        )

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

        if resource == "by-day":
            days = await load_day_rows(db, election_id)
            if not days:
                decoded = await load_published_bytes(db, election_id, kv_sha256=kv_sha256)
                if decoded is None:
                    return None
                one = aggregates_from_decoded(decoded, source_election_id=election_id)
                return build_by_day_series(
                    source_election_id=election_id,
                    days=(("latest", one),),
                )
            rolled: list[tuple[str, dict[str, object]]] = []
            for day in days:
                body = await self._day_body(
                    db,
                    election_id,
                    kv_sha256=kv_sha256,
                    ev_date=day.ev_date,
                    file_id=day.file_id,
                )
                if body is not None:
                    rolled.append((day.ev_date, body))
            return build_by_day_series(source_election_id=election_id, days=tuple(rolled))

        if resource == "county-by-day":
            days = await load_day_rows(db, election_id)
            if not days:
                return None
            rolled = []
            for day in days:
                body = await self._day_body(
                    db,
                    election_id,
                    kv_sha256=kv_sha256,
                    ev_date=day.ev_date,
                    file_id=day.file_id,
                )
                if body is not None:
                    rolled.append((day.ev_date, body))
            return build_county_by_day_series(source_election_id=election_id, days=tuple(rolled))

        file_id = None
        if ev_date is not None:
            file_id = await resolve_file_id_for_day(db, election_id, ev_date)
        decoded = await load_published_bytes(
            db,
            election_id,
            kv_sha256=kv_sha256,
            file_id=file_id,
        )
        if decoded is None:
            return None
        totals = aggregates_from_decoded(
            decoded,
            source_election_id=election_id,
            ev_date=ev_date,
        )
        if resource == "totals":
            return totals
        if resource == "county":
            return {
                "source_election_id": totals.get("source_election_id"),
                "ev_date": totals.get("ev_date"),
                "payload_kind": totals.get("payload_kind"),
                "by_county": totals.get("by_county"),
            }
        if resource == "by-method":
            return slice_by_method(totals)
        return None

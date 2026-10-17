"""Adapters from the read protocols to Workers KV and D1 bindings.

Python KV:
    await self.env.NAMESPACE.get(key)
    await self.env.NAMESPACE.put(key, value, {"expirationTtl": seconds})
https://developers.cloudflare.com/kv/api/read-key-value-pairs/
https://developers.cloudflare.com/kv/api/write-key-value-pairs/

Python D1:
    await self.env.DB.prepare("SELECT ... WHERE resource = ?").bind(resource).first()
https://developers.cloudflare.com/d1/worker-api/d1-database/#prepare
https://developers.cloudflare.com/d1/worker-api/prepared-statements/

One D1 database per election. ``EV_D1_BINDINGS`` is a JSON object of
source_election_id (string) to binding attribute name. This module does
not create databases and does not interpolate the election id into SQL.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from texas_turnout_scraper.civix.ids import require_election_id

logger = logging.getLogger(__name__)

EARLY_VOTE_SQL = "SELECT body FROM early_vote WHERE resource = ?"


class WorkersKvNamespace:
    def __init__(self, namespace: Any) -> None:
        self._namespace = namespace

    async def get(self, key: str) -> str | None:
        value = await self._namespace.get(key)
        if value is None:
            return None
        return str(value)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        # Unset means the contract has not chosen a TTL. Do not pass one.
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


def _body_from_row(row: object) -> str | None:
    if row is None:
        return None
    body: object
    if isinstance(row, dict):
        body = row.get("body")
    else:
        body = getattr(row, "body", None)
    if not isinstance(body, str):
        return None
    return body


class WorkersElectionStore:
    def __init__(self, env: Any, binding_map: dict[str, str]) -> None:
        self._env = env
        self._binding_map = binding_map

    async def fetch_body(self, source_election_id: str, resource: str) -> str | None:
        election_id = require_election_id(source_election_id)
        binding_name = self._binding_map.get(election_id)
        if binding_name is None:
            logger.info("no D1 binding for election=%s", election_id)
            return None
        database = getattr(self._env, binding_name, None)
        if database is None:
            logger.info("D1 binding missing on env election=%s", election_id)
            return None
        statement = database.prepare(EARLY_VOTE_SQL).bind(resource)
        row = await statement.first()
        return _body_from_row(row)

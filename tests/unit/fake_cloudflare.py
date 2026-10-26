"""In-memory stand-ins for Cloudflare D1/KV and the 1Password CLI. No network, no real ``op``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from urllib.parse import unquote

import httpx

from texas_turnout_scraper.consumer_token_admin import ConsumerTokenAdmin
from texas_turnout_scraper.onepassword_keys import (
    OnePasswordAuthError,
    OnePasswordError,
    OnePasswordKeyStore,
)


class FakeCloudflare:
    """D1 is a real in-memory SQLite (D1 is SQLite); KV is a dict of ``(value, params)``."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.kv: dict[str, tuple[str, dict[str, str]]] = {}
        self.fail_d1_insert = False
        self.fail_kv_put = False
        self.sql: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "/d1/database/" in request.url.path:
            return self._d1(request)
        key = unquote(request.url.path.rsplit("/values/", 1)[1])
        if request.method == "PUT":
            if self.fail_kv_put:
                return httpx.Response(500, json={"success": False})
            self.events.append("kv:put")
            self.kv[key] = (request.content.decode(), dict(request.url.params))
        elif request.method == "DELETE":
            self.events.append("kv:delete")
            self.kv.pop(key, None)
        return httpx.Response(200, json={"success": True})

    def _d1(self, request: httpx.Request) -> httpx.Response:
        results: list[dict[str, object]] = []
        for item in json.loads(request.content)["batch"]:
            sql: str = item["sql"]
            self.sql.append(sql)
            if sql.startswith("INSERT INTO consumer_api_token"):
                if self.fail_d1_insert:
                    return httpx.Response(500, json={"success": False})
                self.events.append("d1:insert")
            elif sql.startswith("UPDATE consumer_api_token SET expires_at"):
                self.events.append("d1:update_expiry")
            cursor = self.db.execute(sql, item.get("params") or [])
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
            results.append({"success": True, "results": rows})
        self.db.commit()
        return httpx.Response(200, json={"success": True, "result": results})

    def admin(self) -> ConsumerTokenAdmin:
        client = httpx.Client(
            base_url="https://api.cloudflare.com/client/v4",
            transport=httpx.MockTransport(self.handler),
        )
        return ConsumerTokenAdmin(
            account_id="acct",
            api_token="cf-token",
            d1_database_id="db",
            kv_namespace_id="kv",
            client=client,
        )


class FakeOp:
    """Records every ``op`` call. Stdin is parsed so tests can inspect the template."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[list[str], str | None]] = []
        self.items: dict[str, dict[str, object]] = {}
        self.error: OnePasswordError | None = None
        self.fail_on: Callable[[Sequence[str]], bool] | None = None

    def run(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append((list(args), stdin))
        if self.error is not None and (self.fail_on is None or self.fail_on(args)):
            raise self.error
        verb = args[1]
        if verb == "create":
            self.events.append("op:create")
            template = json.loads(stdin or "{}")
            item_id = f"item{len(self.items) + 1}"
            self.items[item_id] = template
            return json.dumps({"id": item_id, "title": template["title"]})
        if verb == "list":
            tag = args[args.index("--tags") + 1]
            found = [
                {"id": item_id, "title": t["title"]}
                for item_id, t in self.items.items()
                if tag in t["tags"]  # type: ignore[operator]  # reason: test fake, tags is a list
            ]
            return json.dumps(found)
        if verb == "edit":
            self.events.append("op:edit")
            return ""
        if verb == "delete":
            self.events.append("op:archive")
            return ""
        raise AssertionError(f"unexpected op call {args}")

    def store(self) -> OnePasswordKeyStore:
        return OnePasswordKeyStore(self)


def signed_out() -> OnePasswordAuthError:
    return OnePasswordAuthError("1Password CLI is not signed in. Run `op signin` in a terminal.")

"""Cloudflare Python Worker entry: the early-vote read API plus the sync cron trigger.

``fetch`` hands every request to the FastAPI app through ``asgi.fetch``, the explicit
form of ``asgi.entrypoint(app)``. ``scheduled`` runs on the cron trigger and only
dispatches the GitHub Actions sync. Both follow the Python Workers docs:
https://developers.cloudflare.com/workers/languages/python/packages/fastapi/
https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/

The ``workers`` package exists only in the Python Workers runtime, so importing this
module elsewhere leaves ``Default`` as ``None``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ev_api.app import app
from ev_api.cron import run_scheduled


def install_entrypoint() -> type | None:
    try:
        from workers import WorkerEntrypoint, asgi, fetch
    except ImportError:
        return None

    async def send(url: str, headers: dict[str, str], body: dict[str, object]) -> int:
        response = await fetch(url, method="POST", headers=headers, body=json.dumps(body))
        return response.status

    class Default(WorkerEntrypoint):
        async def fetch(self, request):
            return await asgi.fetch(app, request, self.env)

        async def scheduled(self, controller, env, ctx):
            now = datetime.fromtimestamp(controller.scheduledTime / 1000, timezone.utc)
            await run_scheduled(self.env, now, send)

    return Default


Default = install_entrypoint()

"""Cloudflare Python Worker entry for the early-vote aggregate read API."""

from __future__ import annotations

from ev_api.app import app

try:
    from workers import asgi
except ImportError:
    asgi = None  # type: ignore[assignment]


def install_entrypoint() -> type | None:
    if asgi is None:
        return None
    return asgi.entrypoint(app)


Default = install_entrypoint()

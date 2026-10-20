"""Cloudflare Python Workers entrypoint.

Confirmed against the FastAPI package guide, not guessed:

    from workers import asgi
    Default = asgi.entrypoint(app)

https://developers.cloudflare.com/workers/languages/python/packages/fastapi/

``asgi.fetch`` puts the Worker env on the ASGI scope as ``scope["env"]``.
``workers.asgi.env`` is a FastAPI dependency that reads that key. This app
reads ``request.scope["env"]`` in the route so tests can inject stores
without the Workers runtime.

Importing this module outside Workers leaves ``Default`` unset. The
``workers`` package exists only in the Python Workers runtime.
"""

from __future__ import annotations

from ev_api.app import app


def install_entrypoint() -> type | None:
    try:
        from workers import asgi
    except ImportError:
        return None
    return asgi.entrypoint(app)


Default = install_entrypoint()

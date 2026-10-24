"""The guard in conftest.py turns an accidental live request into an immediate failure."""

from __future__ import annotations

import socket

import pytest


def test_a_real_outbound_connection_fails_fast() -> None:
    with pytest.raises(RuntimeError, match="must not open network connections"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_loopback_is_still_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=1)
        client.close()
    finally:
        server.close()

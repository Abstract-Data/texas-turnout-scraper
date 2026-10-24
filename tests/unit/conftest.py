"""Unit tests never reach the network: HTTP is mocked, and a real outbound connection fails fast."""

from __future__ import annotations

import ipaddress
import socket

import pytest

_LOOPBACK_NAMES = {"localhost", ""}


def _is_local(address: object) -> bool:
    if not isinstance(address, tuple):
        return True
    host = str(address[0])
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address):
            raise RuntimeError("unit tests must not open network connections; mock the call")
        return real_connect(self, address)  # type: ignore[arg-type]

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address):
            raise RuntimeError("unit tests must not open network connections; mock the call")
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

"""Unit tests never reach the network: HTTP is mocked, and a real outbound connection fails fast."""

from __future__ import annotations

import ipaddress
import socket
import time
from types import ModuleType

import pytest

from texas_turnout_scraper import early_vote_sync, http_transport, session
from texas_turnout_scraper.civix import evr

_LOOPBACK_NAMES = {"localhost", ""}

# Modules whose request pacing and retry backoff call time.sleep. Against mocked HTTP those
# waits check nothing and were about half the unit suite's wall time.
_PACED_MODULES = (session, http_transport, early_vote_sync, evr)


class _NoSleepTime:
    """Stands in for the ``time`` module inside the paced modules; only ``sleep`` differs."""

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None


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


@pytest.fixture(autouse=True)
def _skip_pacing_sleeps(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("real_sleep"):
        return
    for module in _PACED_MODULES:
        assert isinstance(module.time, ModuleType), f"{module.__name__} no longer imports time"
        monkeypatch.setattr(module, "time", _NoSleepTime())

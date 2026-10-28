"""Sync Worker cron creates a Workflow instead of dispatching GitHub."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from ev_sync.cron import run_scheduled


class _Binding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def create(self, *, id: str, params: dict[str, object]) -> None:
        self.calls.append((id, params))


class _Env:
    def __init__(self, mode: str, binding: _Binding | None) -> None:
        self.EV_SYNC_MODE = mode
        self.EV_SYNC = binding


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_cron_off_skips_workflow_create() -> None:
    binding = _Binding()
    await run_scheduled(_Env("off", binding), _utc("2026-10-31T15:00:00"))
    assert binding.calls == []


@pytest.mark.asyncio
async def test_cron_dry_run_creates_workflow() -> None:
    binding = _Binding()
    await run_scheduled(_Env("dry_run", binding), _utc("2026-10-31T15:00:00"))
    assert len(binding.calls) == 1
    instance_id, params = binding.calls[0]
    assert instance_id.startswith("ev-53815-")
    assert params["mode"] == "dry_run"
    assert params["election_id"] == "53815"

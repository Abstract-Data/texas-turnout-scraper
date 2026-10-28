"""The preview Worker entry serves the ASGI app and answers the cron trigger.

The ``workers`` SDK exists only in the Python Workers runtime, so these tests load
``entry.py`` against a stand-in module that records what the entry hands the runtime.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ENTRY = Path(__file__).resolve().parents[2] / "infra" / "preview" / "worker" / "entry.py"


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def text(self) -> str:
        return ""


class FakeWorkers(types.ModuleType):
    def __init__(self, status: int) -> None:
        super().__init__("workers")
        self.asgi_calls: list[tuple[Any, ...]] = []
        self.fetch_calls: list[tuple[str, dict[str, Any]]] = []
        self.status = status
        workers = self

        class WorkerEntrypoint:
            def __init__(self, ctx: object, env: object) -> None:
                self.ctx = ctx
                self.env = env

        async def asgi_fetch(*args: Any) -> str:
            workers.asgi_calls.append(args)
            return "asgi-response"

        async def fetch(url: str, **kwargs: Any) -> FakeResponse:
            workers.fetch_calls.append((url, kwargs))
            return FakeResponse(workers.status)

        self.WorkerEntrypoint = WorkerEntrypoint
        self.asgi = types.SimpleNamespace(fetch=asgi_fetch)
        self.fetch = fetch


def load_entry(monkeypatch: pytest.MonkeyPatch, workers: types.ModuleType | None) -> Any:
    if workers is None:
        monkeypatch.setitem(sys.modules, "workers", None)
    else:
        monkeypatch.setitem(sys.modules, "workers", workers)
    spec = importlib.util.spec_from_file_location("preview_worker_entry", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


class Env:
    GITHUB_DISPATCH_TOKEN = "github_pat_example_not_real_0123456789"


def test_entry_imports_without_the_workers_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_entry(monkeypatch, None).Default is None


def test_fetch_hands_the_request_to_the_asgi_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from ev_api.app import app

    workers = FakeWorkers(204)
    entry = load_entry(monkeypatch, workers)
    env = Env()
    worker = entry.Default(object(), env)
    assert asyncio.run(worker.fetch("request")) == "asgi-response"
    assert workers.asgi_calls == [(app, "request", env)]


def test_scheduled_posts_the_dispatch_inside_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    workers = FakeWorkers(204)
    entry = load_entry(monkeypatch, workers)
    worker = entry.Default(object(), Env())
    controller = types.SimpleNamespace(
        cron="0 15,16,22,23,3,4 * * *", scheduledTime=ms("2026-10-31T15:00:00")
    )
    asyncio.run(worker.scheduled(controller, worker.env, object()))
    [(url, kwargs)] = workers.fetch_calls
    assert url.endswith("/actions/workflows/early-vote-sync.yml/dispatches")
    assert kwargs["method"] == "POST"
    assert kwargs["headers"]["Authorization"] == f"Bearer {Env.GITHUB_DISPATCH_TOKEN}"
    assert json.loads(kwargs["body"])["inputs"]["trigger"] == "cron"


def test_scheduled_outside_the_window_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    workers = FakeWorkers(204)
    entry = load_entry(monkeypatch, workers)
    worker = entry.Default(object(), Env())
    controller = types.SimpleNamespace(
        cron="0 15,16,22,23,3,4 * * *", scheduledTime=ms("2026-10-31T16:00:00")
    )
    asyncio.run(worker.scheduled(controller, worker.env, object()))
    assert workers.fetch_calls == []

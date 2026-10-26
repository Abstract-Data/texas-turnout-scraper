"""Temporary spike: the Hello-world Workflow loop, the start/status routes and their guards.

Fakes only. The ``workers`` package does not exist off the Workers runtime, so these tests
drive ``run_spike`` with a fake ``step`` that mirrors the documented Python Workflows
decorator (``step.do(name=None, *, concurrent=False, config=None)`` with ``ctx`` injected
into a parameter of that name).
"""

from __future__ import annotations

import inspect
from collections import Counter
from typing import Any

import pytest
from ev_api.app import ReadDeps, create_app
from ev_api.spike_workflow import (
    DEFAULT_STEPS,
    FAILING_STEP_CONFIG,
    MAX_STEPS,
    STEP_CONFIG,
    SpikePlan,
    install_workflow,
    plan_from_payload,
    register_spike_workflow_routes,
    run_spike,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from texas_turnout_scraper.consumer_tokens import hash_token, kv_key_for_hash

ACCESS = {"CF-Access-Client-Id": "id.access", "CF-Access-Client-Secret": "secret-not-real"}
BEARER = {"Authorization": "Bearer agg-secret"}
START = "/spike/workflow/start?suffix=run1"
STATUS = "/spike/workflow/status?id=spike-run1"


class FakeStep:
    """Records every ``step.do`` name and config, then runs the body once."""

    def __init__(self, *, ctx_raises: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.counts: Counter[str] = Counter()
        self._ctx_raises = ctx_raises

    def do(self, name: str | None = None, *, concurrent: bool = False, config=None):
        def decorate(body):
            async def invoke():
                self.calls.append((name, config))
                self.counts[str(name)] += 1
                if "ctx" not in inspect.signature(body).parameters:
                    return await body()
                if self._ctx_raises:
                    raise KeyError("attempt")
                return await body(
                    {
                        "attempt": 1,
                        "step": {"name": name, "count": self.counts[str(name)]},
                        "config": config,
                    }
                )

            return invoke

        return decorate

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def config_for(self, name: str) -> dict[str, Any] | None:
        return next(config for recorded, config in self.calls if recorded == name)


class JsLike:
    """A JS object as Python Workers hands it over: readable only through ``to_py``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_py(self) -> dict[str, Any]:
        return self._data


class FakeInstance:
    def __init__(self, instance_id: str, output: dict[str, Any] | None = None) -> None:
        self.id = instance_id
        self._output = output

    async def status(self) -> JsLike:
        payload: dict[str, Any] = {"status": "running"}
        if self._output is not None:
            payload["output"] = JsLike(self._output)
        return JsLike(payload)


class FakeWorkflow:
    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.instances: dict[str, FakeInstance] = {}
        self._output = output

    async def create(self, *, id: str, params: dict[str, Any]) -> FakeInstance:
        self.created.append((id, params))
        if id in self.instances:
            raise RuntimeError("instance already exists")
        self.instances[id] = FakeInstance(id, self._output)
        return self.instances[id]

    async def get(self, instance_id: str) -> FakeInstance:
        self.gets.append(instance_id)
        if instance_id not in self.instances:
            raise RuntimeError("no such instance")
        return self.instances[instance_id]

    def touched(self) -> bool:
        return bool(self.created or self.gets)


class Kv:
    def __init__(self) -> None:
        self.values = {kv_key_for_hash(hash_token("agg-secret")): "aggregates"}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


def make_deps() -> ReadDeps:
    return ReadDeps(
        kv=Kv(),
        store=None,  # type: ignore[arg-type]
        access_client_id="id.access",
        access_client_secret="secret-not-real",
    )


def workflow_client(binding: FakeWorkflow | None) -> TestClient:
    app = FastAPI()
    deps = make_deps()
    register_spike_workflow_routes(
        app, lambda request: deps, workflow_for_request=lambda request: binding
    )
    return TestClient(app)


async def test_the_loop_runs_a_step_per_index_plus_the_probe_and_the_totals() -> None:
    step = FakeStep()
    totals = await run_spike(SpikePlan(steps=5, fail_at=None, label="run1"), step)
    assert totals["steps_requested"] == 5
    assert totals["steps_run"] == 5
    assert totals["steps_ok"] == 5
    assert totals["steps_caught"] == 0
    assert totals["caught_indexes"] == []
    assert totals["label"] == "run1"
    assert totals["ctx_probe"] == {
        "ok": True,
        "context": {"attempt": 1, "step_name": "spike-ctx-probe", "step_count": 1},
    }
    assert step.names() == [
        "spike-ctx-probe",
        *[f"spike-step-{index:03d}" for index in range(5)],
        "spike-totals",
    ]


async def test_the_deliberately_failing_step_is_caught_and_the_loop_continues() -> None:
    step = FakeStep()
    totals = await run_spike(SpikePlan(steps=6, fail_at=3, label="run1"), step)
    assert totals["steps_run"] == 6
    assert totals["steps_ok"] == 5
    assert totals["steps_caught"] == 1
    assert totals["caught_indexes"] == [3]
    assert "spike-step-005" in step.names(), "the loop must run past the failed step"
    assert step.names()[-1] == "spike-totals"


async def test_the_failing_step_gets_its_own_retry_config() -> None:
    step = FakeStep()
    await run_spike(SpikePlan(steps=4, fail_at=2, label="run1"), step)
    assert step.config_for("spike-step-002") == FAILING_STEP_CONFIG
    assert step.config_for("spike-step-001") == STEP_CONFIG
    assert FAILING_STEP_CONFIG["retries"]["limit"] < STEP_CONFIG["retries"]["limit"]


async def test_a_ctx_injection_failure_is_reported_and_does_not_poison_the_loop() -> None:
    step = FakeStep(ctx_raises=True)
    totals = await run_spike(SpikePlan(steps=3, fail_at=None, label="run1"), step)
    assert totals["ctx_probe"] == {"ok": False, "error_class": "KeyError"}
    assert totals["steps_ok"] == 3


@pytest.mark.parametrize(
    ("payload", "steps", "fail_at"),
    [
        ({}, DEFAULT_STEPS, None),
        (None, DEFAULT_STEPS, None),
        ({"steps": 9999}, MAX_STEPS, None),
        ({"steps": 0}, 1, None),
        ({"steps": -5}, 1, None),
        ({"steps": "12"}, 12, None),
        ({"steps": "many"}, DEFAULT_STEPS, None),
        ({"steps": True}, DEFAULT_STEPS, None),
        ({"steps": 10, "fail_at": 4}, 10, 4),
        ({"steps": 10, "fail_at": 99}, 10, 9),
        ({"steps": 10, "fail_at": -1}, 10, 0),
        ({"steps": 10, "fail_at": "nope"}, 10, None),
    ],
)
def test_the_step_count_is_capped_and_bad_values_fall_back(
    payload: dict[str, Any] | None, steps: int, fail_at: int | None
) -> None:
    plan = plan_from_payload(payload)
    assert (plan.steps, plan.fail_at) == (steps, fail_at)


def test_a_long_label_is_truncated_and_a_missing_one_defaults() -> None:
    assert plan_from_payload({"label": "x" * 200}).label == "x" * 32
    assert plan_from_payload({"label": ""}).label == "spike"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"X-API-Key": "nope"}])
def test_unauthenticated_calls_are_refused_before_the_workflow_is_touched(
    headers: dict[str, str],
) -> None:
    binding = FakeWorkflow()
    client = workflow_client(binding)
    assert client.post(START, headers=headers).status_code == 401
    assert client.get(STATUS, headers=headers).status_code == 401
    assert not binding.touched()


def test_a_consumer_bearer_token_is_accepted_on_both_routes() -> None:
    binding = FakeWorkflow()
    client = workflow_client(binding)
    assert client.post(START, headers=BEARER).status_code == 200
    assert client.get(STATUS, headers=BEARER).status_code == 200


def test_a_started_instance_returns_its_id_steps_and_status() -> None:
    binding = FakeWorkflow()
    report = workflow_client(binding).post(START, headers=ACCESS).json()
    assert report["instance_id"] == "spike-run1"
    assert report["created"] is True
    assert report["duplicate"] is False
    assert report["create_error"] is None
    assert report["steps"] == DEFAULT_STEPS
    assert report["fail_at"] is None
    assert report["status"]["status"] == "running"
    assert binding.created == [
        ("spike-run1", {"steps": DEFAULT_STEPS, "fail_at": None, "label": "run1"})
    ]


def test_the_requested_step_count_is_capped_through_the_route() -> None:
    binding = FakeWorkflow()
    url = "/spike/workflow/start?suffix=run1&steps=9999&fail_at=7"
    report = workflow_client(binding).post(url, headers=ACCESS).json()
    assert report["steps"] == MAX_STEPS
    assert report["fail_at"] == 7
    assert binding.created[0][1]["steps"] == MAX_STEPS


def test_starting_twice_with_the_same_id_reports_the_duplicate() -> None:
    binding = FakeWorkflow()
    client = workflow_client(binding)
    assert client.post(START, headers=ACCESS).json()["created"] is True

    second = client.post(START, headers=ACCESS).json()
    assert second["created"] is False
    assert second["duplicate"] is True
    assert second["create_error"] == {"class": "RuntimeError"}
    assert second["instance_id"] == "spike-run1"
    assert len(binding.instances) == 1


def test_a_create_failure_with_no_existing_instance_is_not_called_a_duplicate() -> None:
    class Broken(FakeWorkflow):
        async def create(self, *, id: str, params: dict[str, Any]) -> FakeInstance:
            self.created.append((id, params))
            raise TimeoutError("upstream")

    binding = Broken()
    report = workflow_client(binding).post(START, headers=ACCESS).json()
    assert report["created"] is False
    assert report["duplicate"] is False
    assert report["create_error"] == {"class": "TimeoutError"}


def test_the_status_route_returns_counts_only() -> None:
    output = {
        "steps_requested": 300,
        "steps_run": 300,
        "steps_ok": 299,
        "steps_caught": 1,
        "fail_at": 7,
        "label": "SYNTHETIC-LABEL",
        "caught_indexes": [7],
    }
    binding = FakeWorkflow(output)
    client = workflow_client(binding)
    client.post(START, headers=ACCESS)

    response = client.get(STATUS, headers=ACCESS)
    body = response.json()
    assert body["found"] is True
    assert body["get_error"] is None
    assert body["status"]["converted"] is True
    assert body["status"]["output_counts"] == {
        "steps_requested": 300,
        "steps_run": 300,
        "steps_ok": 299,
        "steps_caught": 1,
        "fail_at": 7,
    }
    assert "SYNTHETIC-LABEL" not in response.text
    assert "caught_indexes" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_an_unknown_instance_id_reports_the_error_class_only() -> None:
    binding = FakeWorkflow()
    body = workflow_client(binding).get(STATUS, headers=ACCESS).json()
    assert body["found"] is False
    assert body["get_error"] == {"class": "RuntimeError"}
    assert binding.gets == ["spike-run1"]


def test_a_missing_workflow_binding_is_reported_not_crashed() -> None:
    client = workflow_client(None)
    for response in (client.post(START, headers=ACCESS), client.get(STATUS, headers=ACCESS)):
        assert response.status_code == 503
        assert response.json() == {"detail": "workflow_binding_missing"}


@pytest.mark.parametrize(
    "query",
    ["", "?suffix=", "?suffix=Run1", "?suffix=run_1", "?suffix=-run", "?suffix=" + "a" * 41],
)
def test_a_malformed_suffix_never_reaches_the_binding(query: str) -> None:
    binding = FakeWorkflow()
    response = workflow_client(binding).post(f"/spike/workflow/start{query}", headers=ACCESS)
    assert response.status_code == 400
    assert response.json() == {"detail": "bad_request"}
    assert not binding.touched()


@pytest.mark.parametrize("query", ["", "?id=", "?id=run1", "?id=spike-Run1", "?id=spike-"])
def test_a_malformed_instance_id_never_reaches_the_binding(query: str) -> None:
    binding = FakeWorkflow()
    response = workflow_client(binding).get(f"/spike/workflow/status{query}", headers=ACCESS)
    assert response.status_code == 400
    assert not binding.touched()


def test_the_mounted_app_routes_sit_behind_the_same_auth() -> None:
    client = TestClient(create_app(make_deps()))
    assert client.post(START).status_code == 401
    assert client.get(STATUS).status_code == 401
    assert client.post(START, headers={"Authorization": "Bearer nope"}).status_code == 401
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert {"/spike/workflow/start", "/spike/workflow/status"} <= paths


def test_the_workflow_class_is_absent_outside_the_workers_runtime() -> None:
    assert install_workflow() is None

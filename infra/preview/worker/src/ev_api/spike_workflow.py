"""Temporary spike: a Hello-world Python Workflow with a long step loop.

Answers the ADR-014 risks R5, R6, R12 and R13 for the Workflow half of the second
spike. Can a Python Workflow run a 300-step loop, is a failed step catchable so the
loop goes on, and does Workflows reject a duplicate instance id.

``POST /spike/workflow/start?suffix=...`` creates one instance and returns its id.
``GET /spike/workflow/status?id=...`` returns its status and output counts. Both sit
behind the same two auth layers as ``/spike/portal-fetch``. Nothing here reads voter
data, so every value in a step output is a count, an index or a label.

Python Workflows API, docs read 2026-10-26:
https://developers.cloudflare.com/workflows/python/python-workers-api/
https://developers.cloudflare.com/workflows/python/bindings/

Delete this file, its test, ``infra/preview/spike_ingest.tf`` and the mount lines in
``app.py`` and ``entry.py`` to remove the spike.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

DEFAULT_STEPS = 300
MAX_STEPS = 400
MAX_LABEL_CHARS = 32
INSTANCE_PREFIX = "spike-"
WORKFLOW_BINDING = "SPIKE_WORKFLOW"

# WorkflowStepConfig as a plain dict, the form Python Workflows takes for ``config``.
STEP_CONFIG: dict[str, Any] = {
    "retries": {"limit": 2, "delay": "1 second", "backoff": "constant"},
    "timeout": "30 seconds",
}
# The deliberately failing step retries once so the run shows a retry without
# spending the default five attempts at ten seconds apart.
FAILING_STEP_CONFIG: dict[str, Any] = {
    "retries": {"limit": 1, "delay": "1 second", "backoff": "constant"},
    "timeout": "30 seconds",
}

_SUFFIX_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_MAX_REPORTED_INDEXES = 10
_OUTPUT_COUNT_KEYS = frozenset(
    {"steps_requested", "steps_run", "steps_ok", "steps_caught", "fail_at"}
)


class SpikeStepFailure(RuntimeError):
    """Raised by the one deliberately failing step so the loop can catch it."""

    def __init__(self, index: int) -> None:
        super().__init__(f"deliberate spike failure at step {index}")
        self.index = index


@dataclass(frozen=True)
class SpikePlan:
    """What one spike run does. ``fail_at`` is the index of the step that raises."""

    steps: int
    fail_at: int | None
    label: str


def _clamped_int(value: object, *, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return max(low, min(high, number))


def plan_from_payload(payload: object) -> SpikePlan:
    """Clamp a caller-supplied payload into a plan. The only guard on step count."""
    fields: Mapping[str, object] = payload if isinstance(payload, Mapping) else {}
    steps = _clamped_int(fields.get("steps"), low=1, high=MAX_STEPS)
    if steps is None:
        steps = DEFAULT_STEPS
    label = str(fields.get("label") or "spike")[:MAX_LABEL_CHARS]
    return SpikePlan(
        steps=steps,
        fail_at=_clamped_int(fields.get("fail_at"), low=0, high=steps - 1),
        label=label,
    )


async def _ctx_probe(step: Any) -> dict[str, object]:
    """One step that reads the injected ``ctx``, so the loop never depends on it."""

    @step.do("spike-ctx-probe", config=STEP_CONFIG)
    async def probe(ctx):
        return {
            "attempt": int(ctx["attempt"]),
            "step_name": str(ctx["step"]["name"]),
            "step_count": int(ctx["step"]["count"]),
        }

    try:
        return {"ok": True, "context": await probe()}
    except Exception as exc:  # noqa: BLE001 - step errors lose their type over the Workflows RPC
        return {"ok": False, "error_class": type(exc).__name__}


async def _loop_step(step: Any, plan: SpikePlan, index: int) -> bool:
    """Run one loop step. False when it failed and the failure was caught."""
    failing = index == plan.fail_at

    @step.do(
        f"spike-step-{index:03d}",
        config=FAILING_STEP_CONFIG if failing else STEP_CONFIG,
    )
    async def loop_step():
        if failing:
            raise SpikeStepFailure(index)
        return {"index": index, "label": plan.label}

    try:
        await loop_step()
    except Exception:  # noqa: BLE001 - catching here is the R6 question: does the run continue
        return False
    return True


async def run_spike(plan: SpikePlan, step: Any) -> dict[str, object]:
    """The Workflow body, free of any ``workers`` import so it unit-tests with a fake step."""
    probe = await _ctx_probe(step)
    ok = 0
    caught: list[int] = []
    for index in range(plan.steps):
        if await _loop_step(step, plan, index):
            ok += 1
        else:
            caught.append(index)

    totals: dict[str, object] = {
        "steps_requested": plan.steps,
        "steps_run": ok + len(caught),
        "steps_ok": ok,
        "steps_caught": len(caught),
        "caught_indexes": caught[:_MAX_REPORTED_INDEXES],
        "fail_at": plan.fail_at,
        "label": plan.label,
        "ctx_probe": probe,
    }

    @step.do("spike-totals", config=STEP_CONFIG)
    async def totals_step():
        return totals

    return await totals_step()


def install_workflow() -> type | None:
    """The ``SpikeWorkflow`` class, or None outside the Python Workers runtime."""
    try:
        from workers import WorkflowEntrypoint
    except ImportError:
        return None

    class SpikeWorkflow(WorkflowEntrypoint):
        async def run(self, event, step):
            return await run_spike(plan_from_payload(event["payload"]), step)

    return SpikeWorkflow


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """A JS status object reaches Python as a proxy; ``to_py`` is how it converts."""
    if isinstance(value, Mapping):
        return value
    to_py = getattr(value, "to_py", None)
    if to_py is None:
        return None
    converted = to_py()
    return converted if isinstance(converted, Mapping) else None


def _counts(output: Mapping[str, object] | None) -> dict[str, int] | None:
    if output is None:
        return None
    return {
        key: int(value)
        for key, value in output.items()
        if key in _OUTPUT_COUNT_KEYS and isinstance(value, int) and not isinstance(value, bool)
    }


async def _status_summary(instance: Any) -> dict[str, object]:
    try:
        raw = await instance.status()
    except Exception as exc:  # noqa: BLE001 - report the class only, never the message
        return {"error_class": type(exc).__name__}
    fields = _as_mapping(raw)
    if fields is None:
        return {"converted": False, "raw_type": type(raw).__name__}
    status = fields.get("status")
    return {
        "converted": True,
        "status": None if status is None else str(status),
        "output_counts": _counts(_as_mapping(fields.get("output"))),
    }


async def _instance_exists(binding: Any, instance_id: str) -> bool:
    try:
        return await binding.get(instance_id) is not None
    except Exception:  # noqa: BLE001 - get() raises on an unknown id, which is the answer
        return False


async def start_instance(binding: Any, instance_id: str, plan: SpikePlan) -> dict[str, object]:
    """Create one instance. A create failure is reported, and a duplicate id named."""
    report: dict[str, object] = {
        "instance_id": instance_id,
        "steps": plan.steps,
        "fail_at": plan.fail_at,
        "created": False,
        "duplicate": False,
        "create_error": None,
        "status": None,
    }
    params = {"steps": plan.steps, "fail_at": plan.fail_at, "label": plan.label}
    try:
        instance = await binding.create(id=instance_id, params=params)
    except Exception as exc:  # noqa: BLE001 - create() raises when the id is already in use
        report["create_error"] = {"class": type(exc).__name__}
        report["duplicate"] = await _instance_exists(binding, instance_id)
        return report
    report["created"] = True
    report["status"] = await _status_summary(instance)
    return report


def _workflow_from_request(request: Request) -> Any:
    env = request.scope.get("env")
    return None if env is None else getattr(env, WORKFLOW_BINDING, None)


def _no_store(body: dict[str, object], status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


def register_spike_workflow_routes(
    app: FastAPI,
    deps_for_request: Callable[[Request], Any],
    *,
    workflow_for_request: Callable[[Request], Any] = _workflow_from_request,
) -> None:
    async def workflow_start(
        request: Request,
        suffix: str | None = Query(default=None),
        steps: int | None = Query(default=None),
        fail_at: int | None = Query(default=None),
    ) -> Response:
        if suffix is None or _SUFFIX_RE.fullmatch(suffix) is None:
            return _no_store({"detail": "bad_request"}, 400)
        binding = workflow_for_request(request)
        if binding is None:
            return _no_store({"detail": "workflow_binding_missing"}, 503)
        plan = plan_from_payload({"steps": steps, "fail_at": fail_at, "label": suffix})
        return _no_store(await start_instance(binding, INSTANCE_PREFIX + suffix, plan))

    async def workflow_status(
        request: Request,
        instance_id: str | None = Query(default=None, alias="id"),
    ) -> Response:
        if instance_id is None or not instance_id.startswith(INSTANCE_PREFIX):
            return _no_store({"detail": "bad_request"}, 400)
        if _SUFFIX_RE.fullmatch(instance_id[len(INSTANCE_PREFIX) :]) is None:
            return _no_store({"detail": "bad_request"}, 400)
        binding = workflow_for_request(request)
        if binding is None:
            return _no_store({"detail": "workflow_binding_missing"}, 503)
        try:
            instance = await binding.get(instance_id)
        except Exception as exc:  # noqa: BLE001 - report the class only, never the message
            return _no_store(
                {
                    "instance_id": instance_id,
                    "found": False,
                    "get_error": {"class": type(exc).__name__},
                }
            )
        return _no_store(
            {
                "instance_id": instance_id,
                "found": instance is not None,
                "get_error": None,
                "status": None if instance is None else await _status_summary(instance),
            }
        )

    app.add_api_route(
        "/spike/workflow/start", workflow_start, methods=["POST"], response_model=None
    )
    app.add_api_route(
        "/spike/workflow/status", workflow_status, methods=["GET"], response_model=None
    )

"""Shared pytest configuration for unit and integration tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

# The preview read API lives under infra/preview/worker and is not an installable
# extra of the root package. Unit tests import it from this path.
_PREVIEW_WORKER = Path(__file__).resolve().parents[1] / "infra" / "preview" / "worker"
for _worker_src in (
    _PREVIEW_WORKER / "src",
    Path(__file__).resolve().parents[1] / "infra" / "preview" / "sync_worker" / "src",
):
    if _worker_src.is_dir():
        _path = str(_worker_src)
        if _path not in sys.path:
            sys.path.insert(0, _path)


# Hypothesis profiles. Every strategy in this repo generates synthetic data only, so a printed
# falsifying example never holds a real voter. tx-ci is derandomized so CI failures reproduce;
# set HYPOTHESIS_PROFILE=tx-thorough for a deep local run.
settings.register_profile("tx-dev", max_examples=50, deadline=None, stateful_step_count=30)
settings.register_profile(
    "tx-ci",
    max_examples=100,
    derandomize=True,
    deadline=None,
    database=None,
    print_blob=True,
    stateful_step_count=30,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "tx-thorough", max_examples=2000, deadline=None, stateful_step_count=60, print_blob=True
)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("tx-ci" if os.environ.get("CI") else "tx-dev")
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run integration tests against live APIs",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live: mark test as requiring live network access",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="pass --live to run integration tests")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)

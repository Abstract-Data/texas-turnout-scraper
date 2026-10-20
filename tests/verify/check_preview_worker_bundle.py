"""Preview EV API Worker must ship vendored workers-runtime-sdk for Pyodide."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parents[2] / "infra" / "preview" / "worker"
PYTHON_MODULES = WORKER_ROOT / "python_modules"
WORKERS_INIT = PYTHON_MODULES / "workers" / "__init__.py"
PYLOCK = WORKER_ROOT / "pylock.toml"
ENTRY = WORKER_ROOT / "entry.py"


def test_preview_worker_entry_imports_workers_sdk() -> None:
    text = ENTRY.read_text(encoding="utf-8")
    assert "from workers import" in text
    assert "WorkerEntrypoint" in text


def test_preview_worker_vendors_runtime_sdk() -> None:
    assert WORKERS_INIT.is_file(), (
        "Missing python_modules/workers — run "
        "'cd infra/preview/worker && uv sync && uv run pywrangler sync' "
        "and commit python_modules/ + pylock.toml"
    )
    pylock = PYLOCK.read_text(encoding="utf-8")
    match = re.search(
        r'name = "workers-runtime-sdk"\s+version = "([^"]+)"',
        pylock,
    )
    assert match is not None, "pylock.toml must pin workers-runtime-sdk"
    version = tuple(int(part) for part in match.group(1).split("."))
    assert version >= (1, 9, 0), f"workers-runtime-sdk must be >= 1.9, got {match.group(1)}"


def test_preview_worker_pywrangler_sync_is_fresh() -> None:
    """Re-run pywrangler sync; fail if vendored tree drifts from pylock.toml."""
    if not (WORKER_ROOT / "pyproject.toml").is_file():
        return
    result = subprocess.run(
        ["uv", "run", "pywrangler", "sync"],
        cwd=WORKER_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise AssertionError("pywrangler sync failed in infra/preview/worker")

    synced_marker = PYTHON_MODULES / ".synced"
    assert synced_marker.is_file(), "pywrangler sync did not write python_modules/.synced"

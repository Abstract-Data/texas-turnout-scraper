"""Preview EV API Worker must ship vendored workers-runtime-sdk for Pyodide."""

from __future__ import annotations

import re
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parents[2] / "infra" / "preview" / "worker"
PYTHON_MODULES = WORKER_ROOT / "python_modules"
WORKERS_INIT = PYTHON_MODULES / "workers" / "__init__.py"
PYLOCK = WORKER_ROOT / "pylock.toml"
ENTRY = WORKER_ROOT / "entry.py"
UV_LOCK = WORKER_ROOT / "uv.lock"


def test_preview_worker_entry_imports_workers_sdk() -> None:
    text = ENTRY.read_text(encoding="utf-8")
    assert "from workers import" in text
    assert "asgi" in text or "WorkerEntrypoint" in text


def test_preview_worker_vendors_runtime_sdk() -> None:
    assert WORKERS_INIT.is_file(), (
        "Missing python_modules/workers — run "
        "'cd infra/preview/worker && uv sync --frozen && uv run pywrangler sync --force' "
        "and commit python_modules/ + pylock.toml + uv.lock"
    )
    assert UV_LOCK.is_file(), "infra/preview/worker/uv.lock must be committed for frozen CI sync"
    pylock = PYLOCK.read_text(encoding="utf-8")
    match = re.search(
        r'name = "workers-runtime-sdk"\s+version = "([^"]+)"',
        pylock,
    )
    assert match is not None, "pylock.toml must pin workers-runtime-sdk"
    version = tuple(int(part) for part in match.group(1).split("."))
    assert version >= (1, 9, 0), f"workers-runtime-sdk must be >= 1.9, got {match.group(1)}"

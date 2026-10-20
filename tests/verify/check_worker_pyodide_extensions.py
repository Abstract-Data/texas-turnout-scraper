"""Ensure preview Worker PyEmscripten extensions are not gitignored."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_MODULES = REPO_ROOT / "infra" / "preview" / "worker" / "python_modules"
REQUIRED = WORKER_MODULES / "pydantic_core" / "_pydantic_core.cpython-314-wasm32-emscripten.so"


def test_worker_pydantic_core_pyodide_extension_present() -> None:
    assert REQUIRED.is_file(), (
        f"Missing {REQUIRED.relative_to(REPO_ROOT)} — run pywrangler sync and commit "
        "PyEmscripten .so files (see infra/preview/worker/README.md)."
    )


def test_worker_pyodide_extensions_not_empty() -> None:
    wasm = list(WORKER_MODULES.glob("**/*.cpython-*-wasm32-emscripten.so"))
    assert wasm, "No PyEmscripten .so modules under infra/preview/worker/python_modules"

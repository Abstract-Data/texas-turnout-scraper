#!/usr/bin/env bash
# Import smoke test for the preview Worker Pyodide bundle (mirrors deploy-time top-level imports).
# Run from repo root or infra/preview/worker after `uv run pywrangler sync --force`.
set -euo pipefail

worker_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${worker_dir}"

shopt -s nullglob
wasm_exts=(python_modules/**/*.cpython-*-wasm32-emscripten.so)
shopt -u nullglob

if ((${#wasm_exts[@]} == 0)); then
  echo "No PyEmscripten extension modules under python_modules/."
  echo "Run: cd infra/preview/worker && uv run pywrangler sync --force"
  echo "If sync succeeded but .so files are missing from git, check root .gitignore (*.so exception)."
  exit 1
fi

required="python_modules/pydantic_core/_pydantic_core.cpython-314-wasm32-emscripten.so"
if [[ ! -f "${required}" ]]; then
  echo "Missing required extension: ${required}"
  exit 1
fi

pyodide_venv="${worker_dir}/.venv-workers/pyodide-venv"
if [[ ! -f "${pyodide_venv}/bin/activate" ]]; then
  echo "Pyodide venv not found at ${pyodide_venv}; run pywrangler sync first."
  exit 1
fi

# shellcheck source=/dev/null
source "${pyodide_venv}/bin/activate"
export PYTHONPATH="${worker_dir}/python_modules:${worker_dir}/src"

python - <<'PY'
from pydantic_core import ArgsKwargs
from ev_api.app import app

assert app.title == "Early vote read API"
_ = ArgsKwargs((), {})
print("pyodide startup import smoke: OK")
PY

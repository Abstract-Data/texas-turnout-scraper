"""The preview Worker's committed python_modules/ copy of the package must match src/.

`pywrangler sync --force` rebuilds that copy from src/, and terraform-preview.yml fails
on any difference. Without this check, a change to src/ that forgets the vendored copy
only fails later, in a Terraform preview run, and blocks every plan until it is synced.
Fix a failure by copying the file: cp src/<path> infra/preview/worker/python_modules/<path>
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "infra" / "preview" / "worker" / "python_modules"
SKIP_PARTS = {"tmp", "__pycache__"}


def _tracked_package_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", "src/texas_turnout_scraper"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    files = []
    for line in out.splitlines():
        path = Path(line)
        if path.suffix in {".py", ".toml"} and not SKIP_PARTS & set(path.parts):
            files.append(path)
    return files


def test_vendored_worker_bundle_matches_src() -> None:
    stale: list[str] = []
    missing: list[str] = []
    for relative in _tracked_package_files():
        vendored = BUNDLE / Path(*relative.parts[1:])
        if not vendored.is_file():
            missing.append(str(relative))
        elif vendored.read_bytes() != (ROOT / relative).read_bytes():
            stale.append(str(relative))
    assert not stale, f"vendored copy differs from src (copy src over it): {stale}"
    assert not missing, f"module missing from the vendored bundle (copy it in): {missing}"

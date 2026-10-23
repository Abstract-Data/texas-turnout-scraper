"""The preview Worker's committed python_modules/ copy of the package must match src/.

`pywrangler sync --force` rebuilds that copy from src/, and terraform-preview.yml fails
on any difference. Without this check, a change to src/ that forgets the vendored copy
only fails later, in a Terraform preview run, and blocks every plan until it is synced.
Fix a failure by copying the file: cp src/<path> infra/preview/worker/python_modules/<path>
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "infra" / "preview" / "worker" / "python_modules"
SKIP_PARTS = {"tmp", "__pycache__"}


_INIT = Path("src/texas_turnout_scraper/__init__.py")
_VERSION_LINE = re.compile(rb'(?m)^(__version__\s*=\s*)(["\'])[^"\']*\2')


def files_match(relative: Path, src: bytes, vendored: bytes) -> bool:
    """Byte-equal, except the package's own ``__version__`` literal.

    Release Please bumps ``__version__`` in ``src/`` only, so every release PR differs from the
    vendored copy by that one line until the bundle is next resynced. That is not code drift.
    """
    if relative == _INIT:
        src = _VERSION_LINE.sub(rb'\1"<version>"', src)
        vendored = _VERSION_LINE.sub(rb'\1"<version>"', vendored)
    return src == vendored


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
        elif not files_match(relative, (ROOT / relative).read_bytes(), vendored.read_bytes()):
            stale.append(str(relative))
    assert not stale, f"vendored copy differs from src (copy src over it): {stale}"
    assert not missing, f"module missing from the vendored bundle (copy it in): {missing}"


def test_version_only_difference_in_init_is_not_drift() -> None:
    """Release Please bumps __version__ in src/ only; the vendored copy catches up on resync."""
    src = b'"""Package."""\n__version__ = "0.2.7"\n__all__ = ["a"]\n'
    vendored = b'"""Package."""\n__version__ = "0.2.6"\n__all__ = ["a"]\n'
    assert files_match(_INIT, src, vendored)


def test_real_difference_in_init_is_still_drift() -> None:
    src = b'__version__ = "0.2.7"\n__all__ = ["a", "b"]\n'
    vendored = b'__version__ = "0.2.6"\n__all__ = ["a"]\n'
    assert not files_match(_INIT, src, vendored)


def test_version_difference_in_other_modules_is_still_drift() -> None:
    other = Path("src/texas_turnout_scraper/models.py")
    assert not files_match(other, b'__version__ = "2"\n', b'__version__ = "1"\n')

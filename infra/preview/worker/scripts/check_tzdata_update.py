"""Compare the Worker's vendored tzdata with the latest stable PyPI release.

Prints ``current=``, ``latest=`` and ``update=true|false`` lines for $GITHUB_OUTPUT.
Network is limited to one HTTPS GET of the PyPI JSON API for ``tzdata``.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/tzdata/json"
_VERSION = re.compile(r"^\d{4}\.\d+$")
_DIST_INFO = re.compile(r"^tzdata-(\d{4}\.\d+)\.dist-info$")

_WORKER = "infra/preview/worker/"
# Paths a tzdata refresh is allowed to change. Everything else under the worker is drift.
_ALLOWED_DRIFT = (
    re.compile(rf"^{_WORKER}(uv\.lock|pylock\.toml)$"),
    re.compile(rf"^{_WORKER}python_modules/tzdata/"),
    re.compile(rf"^{_WORKER}python_modules/tzdata-\d{{4}}\.\d+\.dist-info/"),
    # Not reproducible across machines; terraform-preview.yml ignores them too.
    re.compile(
        rf"^{_WORKER}python_modules/[^/]+\.dist-info/(RECORD|direct_url\.json|uv_cache\.json)$"
    ),
)


def vendored_version(worker_dir: Path) -> str:
    found = sorted(
        m.group(1)
        for path in (worker_dir / "python_modules").iterdir()
        if (m := _DIST_INFO.match(path.name))
    )
    if len(found) != 1:
        msg = f"expected exactly one vendored tzdata dist-info, found {len(found)}"
        raise SystemExit(msg)
    return found[0]


def version_key(version: str) -> tuple[int, ...]:
    if not _VERSION.match(version):
        msg = f"unexpected tzdata version format: {version!r}"
        raise SystemExit(msg)
    return tuple(int(part) for part in version.split("."))


def is_newer(latest: str, current: str) -> bool:
    return version_key(latest) > version_key(current)


def unexpected_drift(porcelain: str) -> list[str]:
    """Paths in ``git status --porcelain`` output that a tzdata refresh must not touch."""
    unexpected: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not any(pattern.match(path) for pattern in _ALLOWED_DRIFT):
            unexpected.append(path)
    return unexpected


def latest_from_pypi(payload: dict[str, object]) -> str:
    info = payload.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str):
        raise SystemExit("PyPI response is missing info.version")
    return version


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--check-drift"]:
        bad = unexpected_drift(sys.stdin.read())
        for path in bad:
            sys.stdout.write(f"::error::unexpected change after pywrangler sync: {path}\n")
        return 1 if bad else 0
    worker_dir = Path(args[0]) if args else Path(__file__).resolve().parents[1]
    with urllib.request.urlopen(PYPI_URL, timeout=30) as response:
        latest = latest_from_pypi(json.load(response))
    current = vendored_version(worker_dir)
    update = is_newer(latest, current)
    sys.stdout.write(
        f"current={current}\nlatest={latest}\nupdate={'true' if update else 'false'}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

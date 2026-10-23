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


def latest_from_pypi(payload: dict[str, object]) -> str:
    info = payload.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str):
        raise SystemExit("PyPI response is missing info.version")
    return version


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
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

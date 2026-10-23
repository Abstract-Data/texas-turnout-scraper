"""Filter `git status --porcelain` output down to pywrangler drift that should fail CI.

Not reproducible across machines: RECORD hashes, path-dependency direct_url.json and
uv_cache.json under any dist-info. Expected after every release: the package's own
dist-info directory is renamed to the new version and its __init__.py version literal changes.
The importable package trees are still compared byte for byte by
tests/verify/check_vendored_bundle_in_sync.py, so ignoring __init__.py here loses nothing.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable

_PM = r"/python_modules/"
_IGNORED = (
    re.compile(_PM + r"[^/]+[.]dist-info/(RECORD|direct_url[.]json|uv_cache[.]json)$"),
    re.compile(_PM + r"texas_turnout_scraper-[^/]+[.]dist-info(/.*)?$"),
    re.compile(_PM + r"texas_turnout_scraper/__init__[.]py$"),
)


def unexpected_drift(lines: Iterable[str]) -> list[str]:
    return [
        line
        for line in (item.rstrip("\n") for item in lines)
        if line.strip() and not any(pattern.search(line) for pattern in _IGNORED)
    ]


def main() -> int:
    unexpected = unexpected_drift(sys.stdin)
    if unexpected:
        print("Unexpected pywrangler drift after sync:")
        print("\n".join(unexpected))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

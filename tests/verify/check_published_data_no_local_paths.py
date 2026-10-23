"""
Structural verification: tracked data/**/*.json must not leak absolute local paths.
Run with: pytest tests/verify/ -q
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
LOCAL_PATH_RE = re.compile(r"/Users/|/home/|[A-Za-z]:\\\\")


def _tracked_data_json() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "data"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.split("\0") if p.endswith(".json")]


def test_published_data_json_has_no_local_paths():
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _tracked_data_json()
        if LOCAL_PATH_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, f"absolute local paths found in published data: {offenders}"

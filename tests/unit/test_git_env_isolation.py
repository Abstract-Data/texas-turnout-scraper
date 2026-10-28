"""Tests that build their own git repo must not write into the repo pytest runs inside.

A git hook exports GIT_DIR and GIT_INDEX_FILE. Run under pre-commit, a test's throwaway
``git init`` and ``git commit`` once landed in the outer repo and set ``core.bare=true`` there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIT_USING_TESTS = [
    "tests/unit/test_skip_oversize_data.py",
    "tests/unit/test_check_tzdata_update.py",
]


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return out.stdout.strip()


def test_git_using_tests_leave_the_outer_repo_untouched_under_hook_variables(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    (outer / "kept.txt").write_text("outer\n")
    _git(outer, "add", "kept.txt")
    git_dir = outer / ".git"
    env = {
        **os.environ,
        "GIT_DIR": str(git_dir),
        "GIT_INDEX_FILE": str(git_dir / "index"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *GIT_USING_TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    assert _git(outer, "config", "core.bare") == "false"
    assert _git(outer, "rev-list", "--all") == ""
    assert _git(outer, "diff", "--cached", "--name-only") == "kept.txt"

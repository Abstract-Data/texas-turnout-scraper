"""data-refresh must not try to push files over GitHub's 100 MB limit."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "skip_oversize_data.py"
_spec = importlib.util.spec_from_file_location("skip_oversize_data", _SCRIPT)
assert _spec is not None and _spec.loader is not None
skip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skip)

LIMIT = 1_000  # bytes, small for tests


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_untracked_oversize_file_is_removed_and_small_files_kept(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = repo / "data" / "elections" / "civix" / "53813"
    data.mkdir(parents=True)
    big = data / "roster_ev_53813.csv"
    small = data / "audit_ev_53813.json"
    big.write_bytes(b"x" * (LIMIT + 1))
    small.write_bytes(b"{}")
    removed = skip.skip_oversize(repo, Path("data/elections"), LIMIT)
    assert removed == [Path("data/elections/civix/53813/roster_ev_53813.csv")]
    assert not big.exists()
    assert small.exists()
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "roster_ev_53813.csv" in out


def test_tracked_oversize_file_is_restored_not_deleted(repo: Path) -> None:
    data = repo / "data" / "elections"
    data.mkdir(parents=True)
    path = data / "roster_ev_1.csv"
    path.write_bytes(b"old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    path.write_bytes(b"x" * (LIMIT + 1))
    removed = skip.skip_oversize(repo, Path("data/elections"), LIMIT)
    assert removed == [Path("data/elections/roster_ev_1.csv")]
    assert path.read_bytes() == b"old\n"


def test_file_at_the_limit_is_kept(repo: Path) -> None:
    data = repo / "data" / "elections"
    data.mkdir(parents=True)
    path = data / "roster_ev_2.csv"
    path.write_bytes(b"x" * LIMIT)
    assert skip.skip_oversize(repo, Path("data/elections"), LIMIT) == []
    assert path.exists()


def test_default_limit_is_below_the_github_push_limit() -> None:
    assert skip.DEFAULT_LIMIT_BYTES < 100 * 1024 * 1024

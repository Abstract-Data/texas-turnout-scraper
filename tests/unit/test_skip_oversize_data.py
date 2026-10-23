"""data-refresh must not push files over GitHub's 100 MB limit, nor describe held-back data."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "skip_oversize_data.py"
_spec = importlib.util.spec_from_file_location("skip_oversize_data", _SCRIPT)
assert _spec is not None and _spec.loader is not None
skip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skip)

LIMIT = 1_000  # bytes, small for tests
ROOT = Path("data/elections")


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


def _entry(election_id: str, total: int) -> dict[str, object]:
    return {
        "source_election_id": election_id,
        "roster_path": f"civix/{election_id}/roster_ev_{election_id}.csv",
        "total_records": total,
    }


def _write_index(repo: Path, entries: list[dict[str, object]]) -> Path:
    path = repo / ROOT / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_updated": "t", "civix": {"elections": entries}, "legacy": {"elections": []}}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _civix_dir(repo: Path, election_id: str) -> Path:
    path = repo / ROOT / "civix" / election_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_new_oversize_election_is_removed_with_its_audit_and_index_entry(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = _civix_dir(repo, "111")
    (other / "roster_ev_111.csv").write_bytes(b"ok")
    big_dir = _civix_dir(repo, "53813")
    (big_dir / "roster_ev_53813.csv").write_bytes(b"x" * (LIMIT + 1))
    (big_dir / "audit_ev_53813.json").write_text("{}")
    index = _write_index(repo, [_entry("111", 5), _entry("53813", 999)])

    skipped = skip.skip_oversize(repo, ROOT, LIMIT)

    assert skipped == [Path("data/elections/civix/53813/roster_ev_53813.csv")]
    assert not big_dir.exists()
    assert (other / "roster_ev_111.csv").exists()
    ids = [e["source_election_id"] for e in json.loads(index.read_text())["civix"]["elections"]]
    assert ids == ["111"]
    out = capsys.readouterr().out
    assert "roster_ev_53813.csv" in out
    assert "held back: not published" in out


def test_tracked_oversize_election_goes_back_to_its_published_state(repo: Path) -> None:
    directory = _civix_dir(repo, "58315")
    roster = directory / "roster_ev_58315.csv"
    audit = directory / "audit_ev_58315.json"
    roster.write_bytes(b"old\n")
    audit.write_text('{"total": 1}')
    index = _write_index(repo, [_entry("58315", 1), _entry("222", 7)])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")

    roster.write_bytes(b"x" * (LIMIT + 1))
    audit.write_text('{"total": 999}')
    (directory / "gap_report.json").write_text("{}")
    _write_index(repo, [_entry("58315", 999), _entry("222", 8)])

    skip.skip_oversize(repo, ROOT, LIMIT)

    assert roster.read_bytes() == b"old\n"
    assert audit.read_text() == '{"total": 1}'
    assert not (directory / "gap_report.json").exists()
    entries = {
        e["source_election_id"]: e for e in json.loads(index.read_text())["civix"]["elections"]
    }
    assert entries["58315"]["total_records"] == 1
    assert entries["222"]["total_records"] == 8


def test_file_outside_election_layout_only_affects_that_file(repo: Path) -> None:
    data = repo / ROOT
    data.mkdir(parents=True)
    big = data / "roster_ev_1.csv"
    small = data / "other.json"
    big.write_bytes(b"old\n")
    small.write_text("{}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    big.write_bytes(b"x" * (LIMIT + 1))
    small.write_text('{"changed": true}')

    skip.skip_oversize(repo, ROOT, LIMIT)

    assert big.read_bytes() == b"old\n"
    assert small.read_text() == '{"changed": true}'


def test_file_at_the_limit_is_kept(repo: Path) -> None:
    directory = _civix_dir(repo, "2")
    path = directory / "roster_ev_2.csv"
    path.write_bytes(b"x" * LIMIT)
    assert skip.skip_oversize(repo, ROOT, LIMIT) == []
    assert path.exists()


def test_default_limit_is_below_the_github_push_limit() -> None:
    assert skip.DEFAULT_LIMIT_BYTES < 100 * 1024 * 1024


def test_index_bytes_are_restored_when_only_the_held_election_changed(repo: Path) -> None:
    directory = _civix_dir(repo, "58315")
    (directory / "roster_ev_58315.csv").write_bytes(b"old\n")
    index = _write_index(repo, [_entry("58315", 1)])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    committed = index.read_text()

    (directory / "roster_ev_58315.csv").write_bytes(b"x" * (LIMIT + 1))
    payload = json.loads(committed)
    payload["last_updated"] = "later"
    payload["civix"]["elections"][0]["total_records"] = 999
    index.write_text(json.dumps(payload, indent=2) + "\n")

    skip.skip_oversize(repo, ROOT, LIMIT)

    assert index.read_text() == committed


def test_gitignored_oversize_file_does_not_hold_an_election_back(repo: Path) -> None:
    (repo / ".gitignore").write_text("matched_*.csv\n")
    directory = _civix_dir(repo, "7")
    (directory / "roster_ev_7.csv").write_bytes(b"ok")
    (directory / "matched_roster.csv").write_bytes(b"x" * (LIMIT + 1))
    assert skip.skip_oversize(repo, ROOT, LIMIT) == []
    assert (directory / "roster_ev_7.csv").exists()


def test_pathspecs_are_literal(repo: Path) -> None:
    directory = _civix_dir(repo, "8")
    odd = directory / "roster[1].csv"
    odd.write_bytes(b"x" * (LIMIT + 1))
    sibling = repo / ROOT / "keep.csv"
    sibling.write_bytes(b"keep")
    assert "--literal-pathspecs" in _SCRIPT.read_text()
    skip.skip_oversize(repo, ROOT, LIMIT)
    assert sibling.exists()

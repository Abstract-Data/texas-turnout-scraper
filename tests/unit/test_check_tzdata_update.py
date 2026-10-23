"""Version comparison for the Worker tzdata upkeep check."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "preview"
    / "worker"
    / "scripts"
    / "check_tzdata_update.py"
)
_spec = importlib.util.spec_from_file_location("check_tzdata_update", _SCRIPT)
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_is_newer_compares_numerically_not_lexically() -> None:
    assert check.is_newer("2026.10", "2026.9")
    assert check.is_newer("2027.1", "2026.12")
    assert not check.is_newer("2026.5", "2026.5")
    assert not check.is_newer("2026.4", "2026.5")


@pytest.mark.parametrize("bad", ["2026.5\nupdate=true", "v2026.5", "2026", "2026.5rc1", ""])
def test_unexpected_version_formats_are_rejected(bad: str) -> None:
    with pytest.raises(SystemExit):
        check.version_key(bad)


def test_vendored_version_reads_single_dist_info(tmp_path: Path) -> None:
    (tmp_path / "python_modules" / "tzdata-2026.5.dist-info").mkdir(parents=True)
    (tmp_path / "python_modules" / "tzdata").mkdir()
    assert check.vendored_version(tmp_path) == "2026.5"


def test_vendored_version_refuses_ambiguous_bundle(tmp_path: Path) -> None:
    for version in ("2026.4", "2026.5"):
        (tmp_path / "python_modules" / f"tzdata-{version}.dist-info").mkdir(parents=True)
    with pytest.raises(SystemExit):
        check.vendored_version(tmp_path)


def test_latest_from_pypi_requires_version_string() -> None:
    assert check.latest_from_pypi({"info": {"version": "2026.5"}}) == "2026.5"
    with pytest.raises(SystemExit):
        check.latest_from_pypi({"info": {}})


_W = "infra/preview/worker"


def test_unexpected_drift_allows_only_tzdata_lock_and_noise() -> None:
    porcelain = "\n".join(
        [
            f" M {_W}/uv.lock",
            f" M {_W}/pylock.toml",
            f" M {_W}/python_modules/tzdata/zoneinfo/America/Chicago",
            f" D {_W}/python_modules/tzdata-2026.5.dist-info/METADATA",
            f"?? {_W}/python_modules/tzdata-2026.6.dist-info/",
            f" M {_W}/python_modules/fastapi-0.156.1.dist-info/RECORD",
            f" M {_W}/python_modules/texas_turnout_scraper-0.2.7.dist-info/direct_url.json",
        ]
    )
    assert check.unexpected_drift(porcelain) == []


def test_unexpected_drift_flags_other_vendored_changes() -> None:
    porcelain = "\n".join(
        [
            f" M {_W}/python_modules/texas_turnout_scraper/cli/voterfile.py",
            f" M {_W}/python_modules/fastapi-0.156.1.dist-info/METADATA",
            f" M {_W}/uv.lock",
        ]
    )
    assert check.unexpected_drift(porcelain) == [
        f"{_W}/python_modules/texas_turnout_scraper/cli/voterfile.py",
        f"{_W}/python_modules/fastapi-0.156.1.dist-info/METADATA",
    ]


def test_staging_pathspec_stages_new_and_deleted_dist_info(tmp_path: Path) -> None:
    """The workflow's `git add` must stage the old dist-info deletion and the new one."""
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/tzdata-update.yml"
    ).read_text()
    pathspec = "infra/preview/worker/python_modules/tzdata-*.dist-info/*"
    assert f"'{pathspec}'" in workflow

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return done.stdout

    git("init", "-q")
    modules = tmp_path / "infra/preview/worker/python_modules"
    (modules / "tzdata-2026.5.dist-info").mkdir(parents=True)
    (modules / "tzdata-2026.5.dist-info/METADATA").write_text("old")
    (modules / "tzdata").mkdir()
    (modules / "tzdata/zones").write_text("old")
    git("add", "-A")
    git("commit", "-qm", "seed")
    for child in (modules / "tzdata-2026.5.dist-info").iterdir():
        child.unlink()
    (modules / "tzdata-2026.5.dist-info").rmdir()
    (modules / "tzdata-2026.6.dist-info").mkdir()
    (modules / "tzdata-2026.6.dist-info/METADATA").write_text("new")
    (modules / "tzdata/zones").write_text("new")
    git("add", "-A", "--", "infra/preview/worker/python_modules/tzdata", pathspec)
    staged = git("diff", "--cached", "--name-status")
    assert "D\tinfra/preview/worker/python_modules/tzdata-2026.5.dist-info/METADATA" in staged
    assert "A\tinfra/preview/worker/python_modules/tzdata-2026.6.dist-info/METADATA" in staged


def test_workflow_reuses_a_pushed_branch_instead_of_pushing_again() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/tzdata-update.yml"
    ).read_text()
    assert 'gh api "repos/${GITHUB_REPOSITORY}/branches/${branch}"' in workflow
    assert 'if [ "${PUSHED}" != "true" ]; then' in workflow
    assert "Allow GitHub Actions to create and approve pull requests" in workflow
    assert "push --force" not in workflow and "force-with-lease" not in workflow

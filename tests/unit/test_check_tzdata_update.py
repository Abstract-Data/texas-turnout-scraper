"""Version comparison for the Worker tzdata upkeep check."""

from __future__ import annotations

import importlib.util
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

"""Release Please must keep uv.lock's own project version in step with pyproject.toml.

Release Please bumps pyproject.toml and __init__.py but has no uv.lock updater for Python.
Without it the release PR fails `uv sync --locked` ("The lockfile needs to be updated").
The toml `extra-files` updater cannot filter by package name, so release-please-config.json
points at the project's `[[package]]` entry by index. That index shifts when dependencies
are added or removed before it in uv.lock, which would make Release Please rewrite another
package's version. This test fails first and says what to change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_INDEX_PATH = re.compile(r"^\$\.package\[(\d+)\]\.version$")


def _project_name(pyproject: str) -> str:
    match = re.search(r'(?ms)^\[project\]\n.*?^name = "([^"]+)"', pyproject)
    assert match, "pyproject.toml has no [project] name"
    return match.group(1)


def _lock_package_blocks(lock: str) -> list[str]:
    return re.split(r"(?m)^\[\[package\]\]\n", lock)[1:]


def uv_lock_index_error(config: dict[str, object], lock: str, project: str) -> str | None:
    """None when the config's uv.lock updater points at ``project``, else why not."""
    packages = config.get("packages")
    entry = packages.get(".") if isinstance(packages, dict) else None
    extras = entry.get("extra-files", []) if isinstance(entry, dict) else []
    updaters = [e for e in extras if isinstance(e, dict) and e.get("path") == "uv.lock"]
    if len(updaters) != 1:
        return "release-please-config.json needs exactly one extra-files entry for uv.lock"
    updater = updaters[0]
    if updater.get("type") != "toml":
        return 'the uv.lock extra-files entry must have "type": "toml"'
    match = _INDEX_PATH.match(str(updater.get("jsonpath", "")))
    if not match:
        return 'the uv.lock jsonpath must be "$.package[<index>].version"'
    index = int(match.group(1))
    blocks = _lock_package_blocks(lock)
    if index >= len(blocks):
        return f"uv.lock has only {len(blocks)} packages; jsonpath index {index} is out of range"
    if not re.search(rf'(?m)^name = "{re.escape(project)}"$', blocks[index]):
        actual = next(
            (
                i
                for i, b in enumerate(blocks)
                if re.search(rf'(?m)^name = "{re.escape(project)}"$', b)
            ),
            None,
        )
        return (
            f"jsonpath index {index} is not the {project} entry in uv.lock. "
            f"Set it to $.package[{actual}].version in release-please-config.json"
        )
    return None


def test_release_please_updates_the_project_version_in_uv_lock() -> None:
    config = json.loads((ROOT / "release-please-config.json").read_text())
    lock = (ROOT / "uv.lock").read_text()
    project = _project_name((ROOT / "pyproject.toml").read_text())
    assert uv_lock_index_error(config, lock, project) is None, uv_lock_index_error(
        config, lock, project
    )


_LOCK = (
    'version = 1\n\n[[package]]\nname = "alpha"\nversion = "1.0"\n\n'
    '[[package]]\nname = "demo"\nversion = "0.2.6"\nsource = { editable = "." }\n'
)


def _config(path: str) -> dict[str, object]:
    return {
        "packages": {".": {"extra-files": [{"type": "toml", "path": "uv.lock", "jsonpath": path}]}}
    }


def test_correct_index_is_accepted() -> None:
    assert uv_lock_index_error(_config("$.package[1].version"), _LOCK, "demo") is None


def test_shifted_index_is_reported_with_the_fix() -> None:
    error = uv_lock_index_error(_config("$.package[0].version"), _LOCK, "demo")
    assert error is not None
    assert "$.package[1].version" in error


def test_missing_out_of_range_and_wrong_shape_are_reported() -> None:
    assert uv_lock_index_error({"packages": {".": {}}}, _LOCK, "demo")
    assert "out of range" in (
        uv_lock_index_error(_config("$.package[9].version"), _LOCK, "demo") or ""
    )
    assert uv_lock_index_error(_config("$.package[?(@.name=='demo')].version"), _LOCK, "demo")

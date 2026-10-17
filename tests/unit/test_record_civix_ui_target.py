"""The EVR recorder must open the early-vote UI, not the ENR races page."""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record_civix_ui.py"


def _string_assign(name: str) -> str:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == name
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value
    raise AssertionError(name)


def test_entry_url_is_evr_early_vote_ui() -> None:
    url = _string_assign("EVR_UI_URL")
    assert url == "https://goelect.txelections.civixapps.com/ivis-evr-ui/evr"
    assert "ivis-enr-ui" not in url


def test_roster_downloads_stay_blocked() -> None:
    source = SCRIPT.read_text(encoding="utf-8").lower()
    assert "csv download" in source
    assert "voter details" in source
    assert "generate statewide" in source
    assert "blocked early-vote control" in source

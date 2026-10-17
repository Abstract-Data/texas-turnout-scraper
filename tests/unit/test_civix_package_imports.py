"""Core import stays light: optional stacks are not imported with the package."""

from __future__ import annotations

import subprocess
import sys


def test_core_import_does_not_import_optional_stacks() -> None:
    code = """
import sys
import texas_turnout_scraper
from texas_turnout_scraper.civix import CivixClient, CivixElectionRef
# httpx imports rich when that package is installed. The others must stay lazy.
heavy = [
    name
    for name in ("duckdb", "typer", "mcp", "cloudscraper", "questionary", "tqdm")
    if name in sys.modules
]
assert not heavy, heavy
assert CivixClient.__name__ == "CivixClient"
assert CivixElectionRef.__name__ == "CivixElectionRef"
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr

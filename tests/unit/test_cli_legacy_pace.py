"""legacy fetch-all / refresh-all must call legacy_api.list_elections with its real keywords.

data-refresh.yml ran ``tx-turnout legacy refresh-all`` and hit
``TypeError: list_elections() got an unexpected keyword argument 'pace'``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from texas_turnout_scraper import legacy_api
from texas_turnout_scraper.cli import app

runner = CliRunner()


@pytest.fixture
def real_signature_list_elections(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Stub with the same keyword-only signature as the real function."""
    seen: list[float] = []

    def _list_elections(
        *,
        http_backend: str = "cloudscraper",
        pace_seconds: float = 1.0,
    ) -> list[object]:
        seen.append(pace_seconds)
        return []

    monkeypatch.setattr(legacy_api, "list_elections", _list_elections)
    return seen


def test_legacy_refresh_all_passes_pace_seconds(
    tmp_path: Path, real_signature_list_elections: list[float]
) -> None:
    result = runner.invoke(
        app,
        [
            "legacy",
            "refresh-all",
            "--output-dir",
            str(tmp_path / "legacy"),
            "--index-path",
            str(tmp_path / "index.json"),
            "--pace",
            "2.5",
        ],
    )
    assert not isinstance(result.exception, TypeError), result.exception
    assert real_signature_list_elections == [2.5]


def test_legacy_fetch_all_passes_pace_seconds(
    tmp_path: Path, real_signature_list_elections: list[float]
) -> None:
    result = runner.invoke(
        app,
        [
            "legacy",
            "fetch-all",
            "49664",
            "--output-dir",
            str(tmp_path / "legacy"),
            "--index-path",
            str(tmp_path / "index.json"),
            "--pace",
            "3.0",
        ],
    )
    assert not isinstance(result.exception, TypeError), result.exception
    assert real_signature_list_elections == [3.0]

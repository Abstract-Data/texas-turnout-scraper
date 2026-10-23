"""EVR walkthrough: target URL, safety guards, download summary, mocked-page download.

No test here touches the live SOS site. The browser test renders a synthetic page
and skips when Playwright or its Chromium build is not installed.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from texas_turnout_scraper.civix import walkthrough
from texas_turnout_scraper.civix.walkthrough import (
    BLOCKED_CONTROL_TEXT,
    EVR_UI_URL,
    WalkthroughError,
    parse_args,
    resolve_election_label,
    summarize_download,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "civix_evr" / "county_results_page.html"
)
_CSV = (
    b'"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\n'
    b'"TEST VOTER ONE","0000000001","IN-PERSON","0001"\n'
    b'"TEST VOTER TWO","0000000002","MAIL-IN","0002"\n'
)


def test_entry_url_is_evr_early_vote_ui() -> None:
    assert EVR_UI_URL == "https://goelect.txelections.civixapps.com/ivis-evr-ui/evr"
    assert "ivis-enr-ui" not in EVR_UI_URL


def test_roster_controls_stay_blocked_by_default() -> None:
    for text in ("CSV Download", "Voter Details Report", "Generate Statewide Report"):
        assert BLOCKED_CONTROL_TEXT.search(text)
    assert not BLOCKED_CONTROL_TEXT.search("2026 SPECIAL ELECTION SENATE DISTRICT 4")


def test_module_imports_without_playwright() -> None:
    code = (
        "import sys; sys.modules['playwright'] = None; "
        "import texas_turnout_scraper.civix.walkthrough as w; "
        "assert w.EVR_UI_URL"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_resolve_election_label_uses_string_source_id() -> None:
    elections = [
        SimpleNamespace(source_election_id="56181", election_name="SD4 SPECIAL"),
        SimpleNamespace(source_election_id="1", election_name="OTHER"),
    ]
    assert resolve_election_label("56181", elections) == "SD4 SPECIAL"  # type: ignore[arg-type]
    with pytest.raises(WalkthroughError):
        resolve_election_label("999", elections)  # type: ignore[arg-type]


def test_summarize_download_reports_counts_and_fields_not_rows() -> None:
    summary = summarize_download(_CSV)
    assert summary.byte_count == len(_CSV)
    assert summary.row_count == 2
    assert summary.columns == ("VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT")
    assert len(summary.sha256) == 64
    rendered = repr(summary)
    assert "TEST VOTER" not in rendered
    assert "0000000001" not in rendered


def test_redaction_drops_long_digit_runs() -> None:
    assert walkthrough._redact("id 0000000001 ok") == "id [redacted] ok"


def test_parse_args_guards() -> None:
    args = parse_args(["--download-county", "GALVESTON"])
    assert args.source_election_id == "56181"
    assert args.download_county == "GALVESTON"
    with pytest.raises(SystemExit):
        parse_args(["--early-vote-date", "2026-04-20"])
    with pytest.raises(SystemExit):
        parse_args(["--keep-download"])


def _download(county: str, tmp_path: Path, *, keep: bool) -> walkthrough.DownloadSummary:
    pw = pytest.importorskip("playwright.async_api")

    async def go() -> walkthrough.DownloadSummary:
        async with pw.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except pw.Error:
                pytest.skip("Chromium for Playwright is not installed")
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()
            await page.set_content(FIXTURE.read_text(encoding="utf-8"))
            try:
                return await walkthrough.download_county_file(page, county, tmp_path, keep=keep)
            finally:
                await browser.close()

    return asyncio.run(go())


def test_download_county_file_summarizes_and_deletes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        summary = _download("GALVESTON", tmp_path, keep=False)
    assert summary.row_count == 2
    assert summary.columns[:2] == ("VOTER_NAME", "ID_VOTER")
    assert not (tmp_path / "civix-evr-county-download.csv").exists()
    assert "TEST VOTER" not in caplog.text
    assert "0000000001" not in caplog.text


def test_download_county_file_keep_leaves_file(tmp_path: Path) -> None:
    _download("CHAMBERS", tmp_path, keep=True)
    assert (tmp_path / "civix-evr-county-download.csv").read_bytes().count(b"\n") == 3


def test_download_unknown_county_fails_without_clicking(tmp_path: Path) -> None:
    with pytest.raises(WalkthroughError):
        _download("NOWHERE", tmp_path, keep=False)

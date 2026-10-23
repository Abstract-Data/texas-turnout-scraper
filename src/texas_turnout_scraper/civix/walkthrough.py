"""Playwright walkthrough of the Civix EVR early-vote UI.

Derived from the Quick Results recorder ``backend/scripts/record_civix_ui.py``
(revision bae921b9), retargeted from the ENR races page to the EVR early-vote
portal, and moved into this package from the ``scripts/record_civix_ui.py`` draft
in PR #20. No ENR selectors, HTML, or traces are replayed.

The walk opens the EVR UI, selects one election (by ``source_election_id``) and one
early-voting date, and stops on the county turnout table. With ``--download-county``
it also clicks that county's "Voter Details Report" CSV download for the selected
early-voting date (the voters for that date, in-person and mail-in), and records only
its byte count, row count, SHA-256, and header field names. The file is deleted
unless ``--keep-download`` is passed, and the Playwright trace is discarded in that
case because it can capture the response. Statewide reports and every other roster
control stay blocked.

Playwright is not a dependency of this package. Run from the repo root:

  uv run --with playwright python -m playwright install chromium
  uv run --with playwright python -m texas_turnout_scraper.civix.walkthrough \\
      --headless --channel "" --download-county GALVESTON

Recordings go to ``./.recordings/`` (gitignored). They can contain voter data when
``--keep-download`` is used (the trace is then kept too), so do not commit or share
them. Live runs are manual and never part of CI.

PII: ``VOTER_NAME`` and ``ID_VOTER`` values are never logged, printed, or written
to the summary file.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from texas_turnout_scraper.http_transport import HTTP_FETCH_EXCEPTIONS

if TYPE_CHECKING:
    from playwright.async_api import Browser, Locator, Page, Playwright

    from texas_turnout_scraper.models import CivixElection

logger = logging.getLogger(__name__)

# Early-vote UI. The ENR races page is intentionally not the entry URL.
EVR_UI_URL = "https://goelect.txelections.civixapps.com/ivis-evr-ui/evr"

# Small special election (five counties) so the turnout table and file stay small.
DEFAULT_SOURCE_ELECTION_ID = "56181"
DEFAULT_EARLY_VOTE_DATE = "04/20/2026"
DEFAULT_DOWNLOAD_COUNTY = "GALVESTON"

PAGE_GOTO_TIMEOUT_MS = 30_000
ELEMENT_TIMEOUT_MS = 10_000
DOWNLOAD_TIMEOUT_MS = 60_000
PAGE_LOAD_WAIT_MS = 5_000
DESKTOP_VIEWPORT = {"width": 1280, "height": 900}
MOBILE_DEVICE = "Pixel 5"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Roster-level controls. Only the explicit, opt-in county download clicks one.
BLOCKED_CONTROL_TEXT = re.compile(
    r"csv download|voter details|generate statewide",
    re.IGNORECASE,
)
_DIGIT_RUN = re.compile(r"\d{8,}")
_EV_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# A header cell looks like a field name. A data row (a name or an id) never does,
# so a file without a header row records no columns instead of its first voter row.
_HEADER_CELL = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

ELEMENT_DISCOVERY_JS = """
() => {
  const keywords = /election|early|voting|county|date|turnout|report|select/i;
  const out = [];
  const seen = new Set();

  function getAttr(el, name) {
    try { return el.getAttribute(name) || ''; } catch { return ''; }
  }
  function text(el) {
    try { return (el.textContent || '').trim().slice(0, 200); } catch { return ''; }
  }
  function tag(el) {
    try { return el.tagName ? el.tagName.toLowerCase() : ''; } catch { return ''; }
  }

  const candidates = document.querySelectorAll(
    'button, a, mat-select, select, input, [role="button"], [role="combobox"], [role="tab"]'
  );

  candidates.forEach(el => {
    if (el.closest('tbody, table')) return;
    const t = tag(el);
    const title = getAttr(el, 'title');
    const ariaLabel = getAttr(el, 'aria-label');
    const alt = getAttr(el, 'alt');
    const id = getAttr(el, 'id');
    const placeholder = getAttr(el, 'placeholder');
    const cls = (getAttr(el, 'class') || '').slice(0, 150);
    const txt = text(el);
    const sig = `${t}.${id}.${placeholder}.${ariaLabel}.${txt}`;
    if (seen.has(sig)) return;
    seen.add(sig);

    const combined = [title, ariaLabel, alt, placeholder, txt, cls, id].join(' ');
    if (!keywords.test(combined)) return;

    let outer = '';
    try { outer = el.outerHTML.slice(0, 500); } catch { outer = ''; }
    out.push({
      tag: t,
      id: id || null,
      className: cls || null,
      title: title || null,
      ariaLabel: ariaLabel || null,
      placeholder: placeholder || null,
      textContent: txt || null,
      outerHTMLPreview: outer,
    });
  });

  return out;
}
"""


class WalkthroughError(RuntimeError):
    """Expected walkthrough failure. Messages never carry page or voter content."""


@dataclass(frozen=True)
class WalkConfig:
    source_election_id: str
    election_label: str
    early_vote_date: str
    download_county: str | None = None
    keep_download: bool = False


@dataclass(frozen=True)
class DownloadSummary:
    """Counts and field names of a downloaded county file. Never row contents."""

    byte_count: int
    row_count: int
    sha256: str
    columns: tuple[str, ...]


def _playwright_api() -> Any:
    try:
        from playwright import async_api
    except ImportError as exc:
        raise WalkthroughError(
            "playwright is not installed; run with `uv run --with playwright ...`"
        ) from exc
    return async_api


def resolve_election_label(source_election_id: str, elections: Iterable[CivixElection]) -> str:
    """Map a string ``source_election_id`` to the public label the UI lists."""
    wanted = str(source_election_id)
    for election in elections:
        if str(election.source_election_id) == wanted:
            return election.election_name
    raise WalkthroughError("source_election_id was not found in the Civix election index")


def summarize_download(data: bytes) -> DownloadSummary:
    """Count rows and read the header of a downloaded CSV without exposing rows."""
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    try:
        first = next(reader, [])
        row_count = sum(1 for _ in reader)
    except csv.Error as exc:
        raise WalkthroughError("download was not a readable CSV") from exc
    columns = tuple(first) if first and all(_HEADER_CELL.match(cell) for cell in first) else ()
    return DownloadSummary(
        byte_count=len(data),
        row_count=row_count,
        sha256=hashlib.sha256(data).hexdigest(),
        columns=columns,
    )


def summarize_and_discard(saved: Path, *, keep: bool) -> DownloadSummary:
    """Summarize a saved download and delete it even when summarizing fails."""
    try:
        return summarize_download(saved.read_bytes())
    finally:
        if not keep:
            saved.unlink(missing_ok=True)


def _redact(value: str) -> str:
    """Drop long digit runs so control dumps cannot carry a voter id."""
    return _DIGIT_RUN.sub("[redacted]", value)


def _redact_tree(value: object) -> object:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_tree(item) for key, item in value.items()}
    return value


def _transcode_webm_to_mp4(webm: Path, mp4: Path) -> None:
    """Playwright's video artifact is WebM. Transcode it to the requested mp4."""
    mp4.parent.mkdir(parents=True, exist_ok=True)
    log_path = webm.with_suffix(".ffmpeg.log")
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(webm),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    log_path.write_text(result.stderr or "", encoding="utf-8")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)


def trace_destination(config: WalkConfig, recordings_dir: Path) -> Path | None:
    """Where to save the Playwright trace, or None to discard it.

    A trace with snapshots records network activity, which can include the downloaded
    county file's response. When a download runs without ``--keep-download`` the trace
    is discarded so the "file is deleted" promise holds for every artifact.
    """
    if config.download_county is not None and not config.keep_download:
        return None
    return recordings_dir / "civix-evr-trace.zip"


async def _option_index(page: Page, label: str) -> int:
    options = page.locator("mat-option")
    count = await options.count()
    for index in range(count):
        text = " ".join((await options.nth(index).inner_text()).split())
        if text == label:
            return index
    raise WalkthroughError("early-vote option was not listed")


async def _choose_with_keyboard(page: Page, trigger: Locator, label: str, panel_open: bool) -> None:
    """Typeahead for a mat-select when the Links column covers the control."""
    if not panel_open:
        await trigger.focus()
        await page.keyboard.press("Enter")
        await page.wait_for_selector("mat-option", state="visible", timeout=ELEMENT_TIMEOUT_MS)
    await page.keyboard.type(label)
    await page.wait_for_timeout(300)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(400)
    value = " ".join((await trigger.inner_text()).split())
    if value != label:
        raise WalkthroughError("early-vote option was not listed")


async def _choose_option(page: Page, select_id: str, label: str) -> None:
    """Open a mat-select and choose an exact public label (election or date)."""
    if BLOCKED_CONTROL_TEXT.search(label):
        raise WalkthroughError("blocked early-vote control")
    trigger = page.locator(f"#{select_id}")
    await trigger.scroll_into_view_if_needed()
    panel_open = False
    try:
        await trigger.click(timeout=4_000)
        panel_open = True
        await page.wait_for_selector("mat-option", state="visible", timeout=ELEMENT_TIMEOUT_MS)
        await page.wait_for_timeout(800)
        chosen = await _option_index(page, label)
        await page.locator("mat-option").nth(chosen).click(timeout=4_000)
    except _playwright_api().TimeoutError:
        await _choose_with_keyboard(page, trigger, label, panel_open)
    await page.wait_for_timeout(700)
    logger.info("Selected %s", select_id)


async def _activate(page: Page, selector: str) -> None:
    control = page.locator(selector)
    control_text = " ".join((await control.inner_text()).split())
    if BLOCKED_CONTROL_TEXT.search(control_text):
        raise WalkthroughError("blocked early-vote control")
    await control.scroll_into_view_if_needed()
    try:
        await control.click(timeout=4_000)
    except _playwright_api().TimeoutError:
        await control.focus()
        await page.keyboard.press("Enter")


async def save_and_summarize(download: Any, saved: Path, *, keep: bool) -> DownloadSummary:
    """Save a Playwright download, summarize it, and leave no file behind unless ``keep``.

    The cleanup also covers a ``save_as`` that fails partway and leaves a partial file.
    """
    try:
        await download.save_as(str(saved))
        return summarize_and_discard(saved, keep=keep)
    finally:
        if not keep:
            saved.unlink(missing_ok=True)


async def download_county_file(
    page: Page,
    county: str,
    dest_dir: Path,
    *,
    keep: bool,
) -> DownloadSummary:
    """Click one county's CSV Download and summarize the file.

    This is the only path that clicks a roster control, and it needs an explicit
    county. The saved file is removed unless ``keep`` is set.
    """
    target = county.strip().upper()
    row = page.locator("tbody tr").filter(has=page.get_by_role("cell", name=target, exact=True))
    if await row.count() != 1:
        raise WalkthroughError("county row was not found exactly once in the turnout table")
    link = row.get_by_text("CSV Download")
    await link.scroll_into_view_if_needed()
    async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as pending:
        await link.click(timeout=ELEMENT_TIMEOUT_MS)
    download = await pending.value
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = dest_dir / "civix-evr-county-download.csv"
    summary = await save_and_summarize(download, saved, keep=keep)
    logger.info(
        "Downloaded county file bytes=%s rows=%s columns=%s kept=%s",
        summary.byte_count,
        summary.row_count,
        list(summary.columns) or "header-not-recognized",
        keep,
    )
    return summary


async def _walk_early_vote(
    page: Page,
    config: WalkConfig,
    recordings_dir: Path,
) -> DownloadSummary | None:
    """Election, early-vote date, submit. Stops on the county turnout table."""
    await _choose_option(page, "selectElection", config.election_label)
    await _choose_option(page, "votingByDate", config.early_vote_date)
    await _activate(page, "#btnVotingByDate")
    heading = page.get_by_text("Cumulative Percent Early Voting", exact=False).first
    await heading.wait_for(timeout=ELEMENT_TIMEOUT_MS)
    row_count = await page.locator("tbody tr").count()
    logger.info("Early-vote turnout table rows: %s", row_count)
    summary: DownloadSummary | None = None
    if config.download_county is not None:
        summary = await download_county_file(
            page, config.download_county, recordings_dir, keep=config.keep_download
        )
    await page.wait_for_timeout(2000)
    await heading.scroll_into_view_if_needed()
    await page.wait_for_timeout(1500)
    return summary


async def _discover(page: Page, elements_path: Path) -> None:
    elements = await page.evaluate(ELEMENT_DISCOVERY_JS)
    safe_elements = _redact_tree(elements)
    elements_path.write_text(json.dumps(safe_elements, indent=2), encoding="utf-8")
    if not isinstance(safe_elements, list):
        logger.info("Element discovery returned no list")
        return
    logger.info("Found %s early-vote controls", len(safe_elements))
    for index, item in enumerate(safe_elements[:15]):
        if not isinstance(item, dict):
            continue
        logger.info("  [%s] %s id=%s", index, item.get("tag"), item.get("id") or "-")


async def _open_evr(page: Page) -> None:
    logger.info("Navigating to EVR early-vote UI")
    await page.goto(EVR_UI_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
    await page.locator("#selectElection").wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)


async def _record_desktop(
    browser: Browser,
    config: WalkConfig,
    recordings_dir: Path,
) -> DownloadSummary | None:
    video_dir = recordings_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(
        viewport=DESKTOP_VIEWPORT,
        user_agent=BROWSER_USER_AGENT,
        accept_downloads=config.download_county is not None,
        record_video_dir=str(video_dir),
        record_video_size=DESKTOP_VIEWPORT,
    )
    await context.tracing.start(screenshots=True, snapshots=True)
    page: Page | None = None
    video = None
    summary: DownloadSummary | None = None
    try:
        page = await context.new_page()
        video = page.video
        await _open_evr(page)
        await page.screenshot(path=str(recordings_dir / "civix-evr-initial.png"))
        (recordings_dir / "civix-evr-page.html").write_text(await page.content(), encoding="utf-8")
        await _discover(page, recordings_dir / "civix-evr-elements.json")
        summary = await _walk_early_vote(page, config, recordings_dir)
        await page.screenshot(path=str(recordings_dir / "civix-evr-desktop.png"))
    finally:
        trace_path = trace_destination(config, recordings_dir)
        if trace_path is None:
            await context.tracing.stop()
        else:
            await context.tracing.stop(path=str(trace_path))
        if page is not None:
            await page.close()
        if video is not None:
            await video.save_as(str(recordings_dir / "civix-evr-walkthrough.webm"))
        await context.close()
    return summary


async def _record_mobile(
    browser: Browser,
    playwright: Playwright,
    config: WalkConfig,
    recordings_dir: Path,
) -> None:
    if MOBILE_DEVICE not in playwright.devices:
        raise WalkthroughError("mobile device descriptor missing")
    context = await browser.new_context(**playwright.devices[MOBILE_DEVICE], accept_downloads=False)
    page = await context.new_page()
    mobile_config = WalkConfig(
        source_election_id=config.source_election_id,
        election_label=config.election_label,
        early_vote_date=config.early_vote_date,
    )
    try:
        await _open_evr(page)
        await _walk_early_vote(page, mobile_config, recordings_dir)
        await page.screenshot(path=str(recordings_dir / "civix-evr-mobile.png"))
    finally:
        await page.close()
        await context.close()


async def run(
    config: WalkConfig,
    *,
    recordings_dir: Path,
    headless: bool,
    channel: str,
    video_out: Path | None = None,
    frames_dir: Path | None = None,
) -> DownloadSummary | None:
    api = _playwright_api()
    recordings_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with api.async_playwright() as playwright:
            launch_kwargs: dict[str, object] = {"headless": headless, "slow_mo": 250}
            if channel:
                launch_kwargs["channel"] = channel
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                summary = await _record_desktop(browser, config, recordings_dir)
                await _record_mobile(browser, playwright, config, recordings_dir)
            finally:
                await browser.close()
    except api.Error as exc:
        raise WalkthroughError(f"browser step failed ({type(exc).__name__})") from exc

    if summary is not None:
        payload = {"source_election_id": config.source_election_id, **asdict(summary)}
        (recordings_dir / "civix-evr-download.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    if video_out is not None:
        _transcode_webm_to_mp4(recordings_dir / "civix-evr-walkthrough.webm", video_out)
        logger.info("Wrote walkthrough video")
    if frames_dir is not None:
        frames_dir.mkdir(parents=True, exist_ok=True)
        for name in ("civix-evr-desktop.png", "civix-evr-mobile.png"):
            (frames_dir / name).write_bytes((recordings_dir / name).read_bytes())
        logger.info("Wrote desktop and mobile frames")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record the Civix EVR early-vote UI (trace, frames, optional mp4)."
    )
    parser.add_argument("--source-election-id", default=DEFAULT_SOURCE_ELECTION_ID)
    parser.add_argument(
        "--election-label",
        default=None,
        help="Public election label. Default: looked up from the Civix election index.",
    )
    parser.add_argument("--early-vote-date", default=DEFAULT_EARLY_VOTE_DATE, help="MM/DD/YYYY")
    parser.add_argument(
        "--download-county",
        default=None,
        metavar="COUNTY",
        help=f"Opt in to one county CSV download (for example {DEFAULT_DOWNLOAD_COUNTY}).",
    )
    parser.add_argument(
        "--keep-download",
        action="store_true",
        help="Keep the downloaded county file under the recordings dir. It holds voter data.",
    )
    parser.add_argument("--recordings-dir", type=Path, default=Path(".recordings"))
    parser.add_argument("--headless", action="store_true", help="Run headless.")
    parser.add_argument(
        "--channel",
        default="chrome",
        help='Browser channel. Default "chrome". Pass an empty string for bundled Chromium.',
    )
    parser.add_argument("--video-out", type=Path, default=None, help="mp4 or webm destination")
    parser.add_argument("--frames-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if not _EV_DATE.match(args.early_vote_date):
        parser.error("--early-vote-date must be MM/DD/YYYY")
    if args.keep_download and args.download_county is None:
        parser.error("--keep-download requires --download-county")
    return args


def _label_for(args: argparse.Namespace) -> str:
    if args.election_label:
        return str(args.election_label)
    from texas_turnout_scraper.civix.evr import CivixClient

    return resolve_election_label(args.source_election_id, CivixClient().list_elections())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    args = parse_args(argv)
    try:
        config = WalkConfig(
            source_election_id=str(args.source_election_id),
            election_label=_label_for(args),
            early_vote_date=args.early_vote_date,
            download_county=args.download_county,
            keep_download=args.keep_download,
        )
        asyncio.run(
            run(
                config,
                recordings_dir=args.recordings_dir,
                headless=args.headless,
                channel=args.channel,
                video_out=args.video_out,
                frames_dir=args.frames_dir,
            )
        )
    except (
        WalkthroughError,
        subprocess.CalledProcessError,
        OSError,
        ImportError,
        *HTTP_FETCH_EXCEPTIONS,
    ) as exc:
        # WalkthroughError messages are fixed strings; other errors log their type only.
        detail = exc if isinstance(exc, WalkthroughError) else type(exc).__name__
        logger.error("Walkthrough failed (%s)", detail)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

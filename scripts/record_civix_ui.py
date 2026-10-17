#!/usr/bin/env python3
"""Record the Civix early-vote UI so the scraper can follow the live page.

Retargeted from Quick Results ``backend/scripts/record_civix_ui.py``, which opened
the ENR races page. This walkthrough opens the EVR early-vote portal, selects one
election and one early-voting date, and stops on the county turnout table.

It does not open voter-detail reports, statewide roster downloads, or CSV links.
Logs include control ids and row counts only.

On a phone-width viewport the Links column covers the selects, so a pointer
click can time out. The script then uses the mat-select keyboard typeahead.

Run from the repo root:
  python scripts/record_civix_ui.py --headless
  python scripts/record_civix_ui.py --headless --video-out /path/civix-evr-walkthrough.mp4

Playwright decisions (python API, checked against current docs):
  * ``chromium.launch(channel="chrome")`` uses installed Google Chrome.
  * ``new_context(record_video_dir=..., record_video_size=...)`` records the walk.
  * ``page.video.save_as`` runs only after ``page.close()``; the artifact is WebM,
    so ffmpeg transcodes it when ``--video-out`` ends in ``.mp4``.
  * Mobile frames use ``playwright.devices["Pixel 5"]`` on ``new_context``.
  * ``tracing.start(screenshots=True, snapshots=True)`` matches the ENR recorder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from playwright.async_api import Browser, Locator, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Early-vote UI. The ENR races page is intentionally not the entry URL.
EVR_UI_URL = "https://goelect.txelections.civixapps.com/ivis-evr-ui/evr"

# Small certified-style special (five counties) so the turnout table stays aggregate.
WALK_ELECTION = "2026 SPECIAL ELECTION SENATE DISTRICT 4"
WALK_EARLY_VOTE_DATE = "04/20/2026"

PAGE_GOTO_TIMEOUT_MS = 30_000
ELEMENT_TIMEOUT_MS = 10_000
PAGE_LOAD_WAIT_MS = 5_000
DESKTOP_VIEWPORT = {"width": 1280, "height": 900}
MOBILE_DEVICE = "Pixel 5"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Roster-level controls. The walk never clicks these.
BLOCKED_CONTROL_TEXT = re.compile(
    r"csv download|voter details|generate statewide",
    re.IGNORECASE,
)
_DIGIT_RUN = re.compile(r"\d{8,}")

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / ".recordings"

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
    """Playwright's video artifact is WebM. Copy it to the requested mp4."""
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


async def _option_index(page: Page, label: str) -> int:
    options = page.locator("mat-option")
    count = await options.count()
    for index in range(count):
        text = " ".join((await options.nth(index).inner_text()).split())
        if text == label:
            return index
    raise PlaywrightError("early-vote option was not listed")


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
        raise PlaywrightError("early-vote option was not listed")


async def _choose_option(page: Page, select_id: str, label: str) -> None:
    """Open a mat-select and choose an exact public label (election or date)."""
    if BLOCKED_CONTROL_TEXT.search(label):
        raise PlaywrightError("blocked early-vote control")
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
    except PlaywrightTimeoutError:
        await _choose_with_keyboard(page, trigger, label, panel_open)
    await page.wait_for_timeout(700)
    logger.info("Selected %s", select_id)


async def _activate(page: Page, selector: str) -> None:
    control = page.locator(selector)
    control_text = " ".join((await control.inner_text()).split())
    if BLOCKED_CONTROL_TEXT.search(control_text):
        raise PlaywrightError("blocked early-vote control")
    await control.scroll_into_view_if_needed()
    try:
        await control.click(timeout=4_000)
    except PlaywrightTimeoutError:
        await control.focus()
        await page.keyboard.press("Enter")


async def _walk_early_vote(page: Page) -> None:
    """Election, early-vote date, submit. Stops on the county turnout table."""
    await _choose_option(page, "selectElection", WALK_ELECTION)
    await _choose_option(page, "votingByDate", WALK_EARLY_VOTE_DATE)
    await _activate(page, "#btnVotingByDate")
    heading = page.get_by_text("Cumulative Percent Early Voting", exact=False).first
    await heading.wait_for(timeout=ELEMENT_TIMEOUT_MS)
    row_count = await page.locator("tbody tr").count()
    logger.info("Early-vote turnout table rows: %s", row_count)
    await page.wait_for_timeout(2000)
    await heading.scroll_into_view_if_needed()
    await page.wait_for_timeout(1500)


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
        logger.info(
            "  [%s] %s id=%s",
            index,
            item.get("tag"),
            item.get("id") or "-",
        )


async def _open_evr(page: Page) -> None:
    logger.info("Navigating to EVR early-vote UI")
    await page.goto(EVR_UI_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
    await page.locator("#selectElection").wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)


async def _record_desktop(
    browser: Browser,
    *,
    video_dir: Path,
    screenshot_path: Path,
    html_path: Path,
    elements_path: Path,
    trace_path: Path,
    webm_path: Path,
) -> None:
    context = await browser.new_context(
        viewport=DESKTOP_VIEWPORT,
        user_agent=BROWSER_USER_AGENT,
        accept_downloads=False,
        record_video_dir=str(video_dir),
        record_video_size=DESKTOP_VIEWPORT,
    )
    await context.tracing.start(screenshots=True, snapshots=True)
    page: Page | None = None
    video = None
    try:
        page = await context.new_page()
        video = page.video
        await _open_evr(page)
        await page.screenshot(path=str(RECORDINGS_DIR / "civix-evr-initial.png"))
        html_path.write_text(await page.content(), encoding="utf-8")
        await _discover(page, elements_path)
        await _walk_early_vote(page)
        await page.screenshot(path=str(screenshot_path))
    finally:
        await context.tracing.stop(path=str(trace_path))
        if page is not None:
            await page.close()
        if video is not None:
            await video.save_as(str(webm_path))
        await context.close()


async def _record_mobile(browser: Browser, playwright: Playwright, screenshot_path: Path) -> None:
    if MOBILE_DEVICE not in playwright.devices:
        raise PlaywrightError("mobile device descriptor missing")
    context = await browser.new_context(
        **playwright.devices[MOBILE_DEVICE],
        accept_downloads=False,
    )
    page = await context.new_page()
    try:
        await _open_evr(page)
        await _walk_early_vote(page)
        await page.screenshot(path=str(screenshot_path))
    finally:
        await page.close()
        await context.close()


async def run(args: argparse.Namespace) -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    video_dir = RECORDINGS_DIR / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    desktop_path = RECORDINGS_DIR / "civix-evr-desktop.png"
    mobile_path = RECORDINGS_DIR / "civix-evr-mobile.png"
    webm_path = RECORDINGS_DIR / "civix-evr-walkthrough.webm"

    async with async_playwright() as playwright:
        launch_kwargs: dict[str, object] = {"headless": args.headless, "slow_mo": 250}
        if args.channel:
            launch_kwargs["channel"] = args.channel
        browser = await playwright.chromium.launch(**launch_kwargs)
        try:
            await _record_desktop(
                browser,
                video_dir=video_dir,
                screenshot_path=desktop_path,
                html_path=RECORDINGS_DIR / "civix-evr-page.html",
                elements_path=RECORDINGS_DIR / "civix-evr-elements.json",
                trace_path=RECORDINGS_DIR / "civix-evr-trace.zip",
                webm_path=webm_path,
            )
            await _record_mobile(browser, playwright, mobile_path)
        finally:
            await browser.close()

    if args.video_out is not None:
        _transcode_webm_to_mp4(webm_path, args.video_out)
        logger.info("Wrote walkthrough video")
    if args.frames_dir is not None:
        args.frames_dir.mkdir(parents=True, exist_ok=True)
        (args.frames_dir / "civix-evr-desktop.png").write_bytes(desktop_path.read_bytes())
        (args.frames_dir / "civix-evr-mobile.png").write_bytes(mobile_path.read_bytes())
        logger.info("Wrote desktop and mobile frames")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record the Civix EVR early-vote UI (trace, frames, optional mp4)."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless. Default is headed, matching the ENR recorder.",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help='Browser channel. Default "chrome". Pass an empty string for bundled Chromium.',
    )
    parser.add_argument("--video-out", type=Path, default=None, help="mp4 or webm destination")
    parser.add_argument(
        "--frames-dir",
        type=Path,
        default=None,
        help="Directory for civix-evr-desktop.png and civix-evr-mobile.png",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except (PlaywrightError, subprocess.CalledProcessError, OSError) as exc:
        logger.error("Recording failed (%s)", type(exc).__name__)
        sys.exit(1)

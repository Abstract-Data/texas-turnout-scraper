"""Generate committed fixture proof PNGs for PR review (run locally or in CI)."""

from __future__ import annotations

import logging
from pathlib import Path

from reports.karl_rove.build import write_outputs
from reports.karl_rove.fixtures import example_report

logger = logging.getLogger(__name__)

PROOF_DIR = Path(__file__).resolve().parent / "proof"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    out = Path("dist/karl-rove-proof")
    data = example_report()
    write_outputs(data, html_path=out / "karl-rove-report.html", pdf_path=out / "karl-rove-report.pdf")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright required: uv sync --extra reports && playwright install chromium")
        raise SystemExit(1) from None

    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    html_url = (out / "karl-rove-report.html").resolve().as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.goto(html_url)
        page.screenshot(path=str(PROOF_DIR / "fixture-desktop.png"), full_page=True)
        page.emulate_media(media="print")
        page.screenshot(path=str(PROOF_DIR / "fixture-print.png"), full_page=True)
        browser.close()
    logger.info("proof assets written to %s", PROOF_DIR)


if __name__ == "__main__":
    main()
